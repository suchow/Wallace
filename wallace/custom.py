"""Import custom routes into the experiment server."""

from flask import Blueprint, request, Response, send_from_directory, \
    jsonify, render_template

from psiturk.psiturk_config import PsiturkConfig
from psiturk.user_utils import PsiTurkAuthorization
from psiturk.db import init_db

# Database setup
from psiturk.db import db_session as session_psiturk
from psiturk.models import Participant
from json import dumps

from wallace import db, models

import imp
import inspect
import urllib
from operator import attrgetter
import datetime

from rq import Queue
from worker import conn

from sqlalchemy import and_, exc

import traceback

# Load the configuration options.
config = PsiturkConfig()
config.load_config()
myauth = PsiTurkAuthorization(config)

# Explore the Blueprint.
custom_code = Blueprint(
    'custom_code', __name__,
    template_folder='templates',
    static_folder='static')

# Initialize the Wallace database.
session = db.get_session()

# Connect to the Redis queue for notifications.
q = Queue(connection=conn)

# Specify the experiment.
try:
    exp = imp.load_source('experiment', "wallace_experiment.py")
    classes = inspect.getmembers(exp, inspect.isclass)
    exps = [c for c in classes
            if (c[1].__bases__[0].__name__ in "Experiment")]
    this_experiment = exps[0][0]
    mod = __import__('wallace_experiment', fromlist=[this_experiment])
    experiment = getattr(mod, this_experiment)
except ImportError:
    print "Error: Could not import experiment."


@custom_code.route('/robots.txt')
def static_from_root():
    """"Serve robots.txt from static file."""
    return send_from_directory('static', request.path[1:])


@custom_code.route('/launch', methods=['POST'])
def launch():
    """Launch the experiment."""
    exp = experiment(db.init_db(drop_all=False))
    exp.log("Launch route hit, laucnhing experiment", "-----")
    exp.log("Experiment launching, initiailizing tables", "-----")
    init_db()
    exp.log("Experiment launching, opening recruitment", "-----")
    exp.recruiter().open_recruitment(n=exp.initial_recruitment_size)

    session_psiturk.commit()
    session.commit()

    exp.log("Experiment successfully launched, retuning status 200", "-----")
    data = {"status": "success"}
    js = dumps(data)
    return Response(js, status=200, mimetype='application/json')


@custom_code.route('/compute_bonus', methods=['GET'])
def compute_bonus():
    """Overide the psiTurk compute_bonus route."""
    return Response(dumps({"bonusComputed": "success"}), status=200)


@custom_code.route('/summary', methods=['GET'])
def summary():
    """Summarize the participants' status codes."""
    exp = experiment(session)
    data = {"status": "success", "summary": exp.log_summary()}
    js = dumps(data)
    return Response(js, status=200, mimetype='application/json')


@custom_code.route('/worker_complete', methods=['GET'])
def worker_complete():
    """Overide the psiTurk worker_complete route.

    This skirts around an issue where the participant's status reverts to 3
    because of rogue calls to this route. It does this by changing the status
    only if it's not already >= 100.
    """
    exp = experiment(session)

    if 'uniqueId' not in request.args:
        resp = {"status": "bad request"}
        return jsonify(**resp)
    else:
        unique_id = request.args['uniqueId']
        exp.log("Completed experiment %s" % unique_id)
        try:
            user = Participant.query.\
                filter(Participant.uniqueid == unique_id).one()
            if user.status < 100:
                user.status = 3
                user.endhit = datetime.datetime.now()
                session_psiturk.add(user)
                session_psiturk.commit()
            status = "success"
        except exc.SQLAlchemyError:
            status = "database error"
        resp = {"status": status}
        return jsonify(**resp)


"""
Database accessing routes
"""


@custom_code.route("/node", methods=["POST", "GET"])
def node():
    """ Send GET or POST requests to the node table.

    POST requests call the node_post_request method
    in Experiment, which, by deafult, makes a new node
    for the participant. This request returns a
    description of the new node.
    Required arguments: participant_id

    GET requests call the node_get_request method
    in Experiment, which, by default, call the
    neighbours method of the node making the request.
    This request returns a list of descriptions of
    the nodes (even if there is only one).
    Required arguments: participant_id, node_id
    Optional arguments: type, failed, connection
    """
    # load the experiment
    exp = experiment(session)

    # get the participant_id
    try:
        participant_id = request.values["participant_id"]
        key = participant_id[0:5]
    except:
        exp.log("/node request failed: participant_id not specified")
        page = error_page(error_type="/node, participant_id not specified")
        js = dumps({"status": "error", "html": page})
        return Response(js, status=403, mimetype='application/json')

    if request.method == "GET":

        # get the node_id
        try:
            node_id = request.values["node_id"]
            if not node_id.isdigit():
                exp.log("/node GET request failed: non-numeric node_id: {}".format(node_id), key)
                page = error_page(error_type="/node GET, non-numeric node_id")
                js = dumps({"status": "error", "html": page})
                return Response(js, status=403, mimetype='application/json')
        except:
            exp.log("/node GET request failed: node_id not specified", key)
            page = error_page(error_type="/node GET, node_id not specified")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

        exp.log("Received a /node GET request from node {}".format(node_id), key)

        # get type and check it is in trusted_strings
        try:
            node_type = request.values["node_type"]
            exp.log("type specified", key)
            if node_type in exp.trusted_strings:
                node_type = exp.evaluate(node_type)
                exp.log("node_type in trusted_strings", key)
            else:
                exp.log("/node GET request failed: untrusted node_type {}".format(node_type), key)
                page = error_page(error_type="/node GET, unstrusted node_type")
                js = dumps({"status": "error", "html": page})
                return Response(js, status=403, mimetype='application/json')
        except:
            node_type = models.Node
            exp.log("type not specified, defaulting to Node", key)

        # get failed
        try:
            failed = request.values["failed"]
            exp.log("failed specified", key)
        except:
            failed = False
            exp.log("failed not specified, defaulting to False", key)

        # get connection
        try:
            connection = request.values["connection"]
            exp.log("connection specified", key)
        except:
            connection = "to"
            exp.log("connection not specified, defaulting to 'to'", key)

        # execute the experiment method
        exp.log("Getting requested nodes", key)
        try:
            nodes = exp.node_get_request(participant_id=participant_id, node_id=node_id, node_type=node_type, failed=failed, connection=connection)
            session.commit()
            exp.log("node_get_request successful", key)
        except:
            session.commit()
            print(traceback.format_exc())
            exp.log("/node GET request failed: error in node_get_request", key)
            page = error_page(error_type="/node GET, node_get_request error")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

        # parse the data to return
        exp.log("Creating data to return", key)
        data = []
        for n in nodes:
            data.append({
                "id": n.id,
                "type": n.type,
                "network_id": n.network_id,
                "creation_time": n.creation_time,
                "time_of_death": n.receive_time,
                "failed": n.failed,
                "participant_id": n.participant_id,
                "property1": n.property1,
                "property2": n.property2,
                "property3": n.property3,
                "property4": n.property4,
                "property5": n.property5
            })
        data = {"status": "success", "nodes": data}

        # return the data
        exp.log("Data successfully created, returning.", key)
        js = dumps(data, default=date_handler)
        return Response(js, status=200, mimetype='application/json')

    elif request.method == "POST":

        # get the participant
        participant = Participant.query.\
            filter(Participant.uniqueid == participant_id).all()
        if len(participant) == 0:
            exp.log("Error: No participants with that id. Returning status 403", key)
            page = error_page(error_text="You cannot continue because your worker id does not match anyone in our records.", error_type="/agents no participant found")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')
        if len(participant) > 1:
            exp.log("Error: Multiple participants with that id. Returning status 403", key)
            page = error_page(error_text="You cannot continue because your worker id is the same as someone else's.", error_type="/agents multiple participants found")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')
        participant = participant[0]

        check_for_duplicate_assignments(participant)

        # make sure their status is 1 or 2, otherwise they must have come here by mistake
        exp.log("Checking participant status", key)
        if participant.status not in [1, 2]:
            exp.log("Error: Participant status is {} they should not have been able to contact this route. Returning error_wallace.html.".format(participant.status), key)
            if participant.status in [3, 4, 5, 100, 101, 102, 105]:
                page = error_page(participant=participant, error_text="You cannot continue because we have received a notification from AWS that you have already submitted the assignment.'", error_type="/agents POST, status = {}".format(participant.status))
            elif participant.status == 103:
                page = error_page(participant=participant, error_text="You cannot continue because we have received a notification from AWS that you have returned the assignment.'", error_type="/agents POST, status = {}".format(participant.status))
            elif participant.status == 104:
                page = error_page(participant=participant, error_text="You cannot continue because we have received a notification from AWS that your assignment has expired.", error_type="/agents POST, status = {}".format(participant.status))
            elif participant.status == 106:
                page = error_page(participant=participant, error_text="You cannot continue because we have received a notification from AWS that your assignment has been assigned to someone else.", error_type="/agents POST, status = {}".format(participant.status))
            else:
                page = error_page(participant=participant, error_type="/agents POST, status = {}".format(participant.status))

            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

        # execute the experiment method
        exp.log("All checks passed: posting new node", key)
        try:
            node = exp.node_post_request(participant_id=participant_id)
            exp.log("node_post_request finished without error", key)
            session.commit()
        except:
            session.commit()
            print(traceback.format_exc())
            exp.log("node_post_request failed", key)
            page = error_page(error_type="/node POST, node_post_request error")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

        # if it returns None return an error
        if node is None:
            exp.log("Node not made for participant, hopefully because they are finished, returning status 403", key)
            js = dumps({"status": "error"})
            return Response(js, status=403)

        # parse the data for returning
        exp.log("Node successfully posted, creating data to return", key)
        data = {
            "id": node.id,
            "type": node.type,
            "network_id": node.network_id,
            "creation_time": node.creation_time,
            "time_of_death": node.time_of_death,
            "failed": node.failed,
            "participant_id": node.participant_id,
            "property1": node.property1,
            "property2": node.property2,
            "property3": node.property3,
            "property4": node.property4,
            "property5": node.property5
        }
        data = {"status": "success", "node": data}

        # return the data
        exp.log("Data successfully created, returning.", key)
        js = dumps(data, default=date_handler)
        return Response(js, status=200, mimetype='application/json')


@custom_code.route("/vector", methods=["GET", "POST"])
def vector():
    """ Send GET or POST requests to the vector table.

    POST requests call the vector_post_request method
    in Experiment, which, by deafult, prompts one node to
    connect to or from another. This request returns a list of
    descriptions of the new vectors created.
    Required arguments: participant_id, node_id, other_node_id
    Optional arguments: direction.

    GET requests call the vector_get_request method
    in Experiment, which, by default, calls the node's
    vectors method if no other_node_id is specified,
    or its is_connected method if the other_node_id is
    specified. This request returns a list of
    descriptions of the vectors (even if there is only one),
    or a boolean, respectively.
    Required arguments: participant_id, node_id
    Optional arguments: other_node_id, failed, direction, vector_failed
    """
    # load the experiment
    exp = experiment(session)

    # get the participant_id
    try:
        participant_id = request.values["participant_id"]
        key = participant_id[0:5]
    except:
        exp.log("/vector request failed: participant_id not specified")
        page = error_page(error_type="/vector, participant_id not specified")
        js = dumps({"status": "error", "html": page})
        return Response(js, status=403, mimetype='application/json')
    exp.log("Received a vector request", key)

    # get the node_id
    try:
        node_id = request.values["node_id"]
        if not node_id.isdigit():
            exp.log(
                "/vector request failed: non-numeric node_id: {}"
                .format(node_id), key)
            page = error_page(error_type="/vector, non-numeric node_id")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')
    except:
        exp.log("/vector request failed: node_id not specified", key)
        page = error_page(error_type="/vector, node_id not specified")
        js = dumps({"status": "error", "html": page})
        return Response(js, status=403, mimetype='application/json')

    if request.method == "GET":
        exp.log("vector request is a GET request", key)

        # get the other_node_id
        try:
            other_node_id = request.values["other_node_id"]
            exp.log("other_node_id specified", key)
            if not other_node_id.isdigit():
                exp.log(
                    "/vector GET request failed: non-numeric other_node_id: {}"
                    .format(other_node_id), key)
                page = error_page(error_type="/vector GET, non-numeric other_node_id")
                js = dumps({"status": "error", "html": page})
                return Response(js, status=403, mimetype='application/json')
        except:
            other_node_id = None
            exp.log("other_node_id not specified", key)

        # if other_node_id is not None we return if the node
        # is_connected to the other_node
        if other_node_id is not None:
            # get the direction
            try:
                direction = request.values["direction"]
                exp.log("direction specified", key)
            except:
                exp.log("direction not specified, setting to 'to'", key)
                direction = "to"

            # get the vector_failed
            try:
                vector_failed = request.values["vector_failed"]
                exp.log("vector_failed specified", key)
            except:
                vector_failed = False
                exp.log("vector_failed not specified, setting to 'False'", key)

            # execute the experiment method
            exp.log("Running vector_get_request", key)
            try:
                is_connected = exp.vector_get_request(participant_id=participant_id, node_id=node_id, other_node_id=other_node_id, direction=direction, vector_failed=vector_failed)
                session.commit()
            except:
                session.commit()
                print(traceback.format_exc())
                exp.log("vector_get_request failed")
                page = error_page(error_type="/vector GET, vector_get_request error")
                js = dumps({"status": "error", "html": page})
                return Response(js, status=403, mimetype='application/json')

            # return the data
            exp.log("vector_get_request successful", key)
            data = {"status": "success", "is_connected": is_connected}
            exp.log("vector data successfully created, returning.", key)
            js = dumps(data, default=date_handler)
            return Response(js, status=200, mimetype='application/json')

        # if other_node_id is None, we return a list of vectors
        else:

            # get the direction
            try:
                direction = request.values["direction"]
                exp.log("direction specified", key)
            except:
                direction = "all"
                exp.log("direction not specified, setting to 'all'", key)

            # get failed
            try:
                failed = request.values["failed"]
                exp.log("failed specified", key)
            except:
                failed = False
                exp.log("failed not specified, setting to 'False'", key)

            # execute the experiment method
            try:
                vectors = exp.vector_get_request(participant_id=participant_id, node_id=node_id, other_node_id=other_node_id, direction=direction, failed=failed)
                session.commit()
            except:
                session.commit()
                print(traceback.format_exc())
                exp.log("vector_get_request failed")
                page = error_page(error_type="/vector GET, vector_get_request error")
                js = dumps({"status": "error", "html": page})
                return Response(js, status=403, mimetype='application/json')
            exp.log("vector_get_request successful", key)

            # parse the data for returning
            exp.log("Creating vector data to return", key)
            data = []
            for v in vectors:
                data.append({
                    "id": v.id,
                    "origin_id": v.origin_id,
                    "destination_id": v.destination_id,
                    "info_id": v.info_id,
                    "network_id": v.network_id,
                    "creation_time": v.creation_time,
                    "failed": v.failed,
                    "time_of_death": v.time_of_death,
                    "property1": v.property1,
                    "property2": v.property2,
                    "property3": v.property3,
                    "property4": v.property4,
                    "property5": v.property5
                })
            data = {"status": "success", "vectors": data}

            # return the data
            exp.log("Data successfully created, returning.", key)
            js = dumps(data, default=date_handler)
            return Response(js, status=200, mimetype='application/json')

    elif request.method == "POST":
        exp.log("vector request is a POST request", key)

        # get the other_node_id
        try:
            other_node_id = request.values["other_node_id"]
            if not other_node_id.isdigit():
                exp.log(
                    "/vector POST request failed: non-numeric other_node_id: {}"
                    .format(node_id), key)
                page = error_page(error_type="/vector POST, non-numeric other_node_id")
                js = dumps({"status": "error", "html": page})
                return Response(js, status=403, mimetype='application/json')
        except:
            exp.log("/vector POST request failed: other_node_id not specified", key)
            page = error_page(error_type="/vector, node_id not specified")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

        # get the direction
        try:
            direction = request.values["direction"]
            exp.log("direction specified", key)
        except:
            direction = "to"
            exp.log("direction not specified, setting to 'to'", key)

        # execute the experiment method
        try:
            vectors = exp.vector_post_request(participant_id=participant_id, node_id=node_id, other_node_id=other_node_id, direction=direction)
            session.commit()
        except:
            session.commit()
            print(traceback.format_exc())
            exp.log("vector_post_request failed")
            page = error_page(error_type="/vector POST, vector_post_request error")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

        # parse the data for returning
        exp.log("Creating vector data to return", key)
        data = []
        for v in vectors:
            data.append({
                "id": v.id,
                "origin_id": v.origin_id,
                "destination_id": v.destination_id,
                "info_id": v.info_id,
                "network_id": v.network_id,
                "creation_time": v.creation_time,
                "failed": v.failed,
                "time_of_death": v.time_of_death,
                "property1": v.property1,
                "property2": v.property2,
                "property3": v.property3,
                "property4": v.property4,
                "property5": v.property5
            })

        # return data
        exp.log("Returning the data", key)
        data = {"status": "success", "vectors": data}
        js = dumps(data, default=date_handler)
        return Response(js, status=200, mimetype='application/json')


@custom_code.route("/info", methods=["GET", "POST"])
def info():
    """ Send GET or POST requests to the info table.

    POST requests call the info_post_request method
    in Experiment, which, by deafult, creates a new
    info of the specified type. This request returns
    a description of the new info. To create infos
    of custom classes you need to add the name of the
    class to the trusted_strings variable in the
    experiment file.
    Required arguments: participant_id, node_id, contents.
    Optional arguments: type.

    GET requests call the info_get_request method
    in Experiment, which, by default, calls the node's
    infos method. This request returns a list of
    descriptions of the infos (even if there is only one).
    Required arguments: participant_id, node_id
    Optional arguments: info_id, type.
    """
    # load the experiment
    exp = experiment(session)

    # get the participant_id
    try:
        participant_id = request.values["participant_id"]
        key = participant_id[0:5]
    except:
        exp.log("/info request failed: participant_id not specified")
        page = error_page(error_type="/info, participant_id not specified")
        js = dumps({"status": "error", "html": page})
        return Response(js, status=403, mimetype='application/json')

    # get the node_id
    try:
        node_id = request.values["node_id"]
        if not node_id.isdigit():
            exp.log(
                "/info request failed: non-numeric node_id: {}".format(node_id),
                key)
            page = error_page(error_type="/info, non-numeric node_id")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')
    except:
        exp.log("/info request failed: node_id not specified", key)
        page = error_page(error_type="/info, node_id not specified")
        js = dumps({"status": "error", "html": page})
        return Response(js, status=403, mimetype='application/json')

    # get type
    try:
        info_type = request.values["info_type"]
    except:
        info_type = None
    if info_type is not None:
        if info_type in exp.trusted_strings:
            info_type = exp.evaluate(info_type)
        else:
            exp.log("/info request failed: bad type {}".format(info_type), key)
            page = error_page(error_type="/info, bad type")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

    if request.method == "GET":

        # get the info_id
        try:
            info_id = request.values["info_id"]
            if not info_id.isdigit():
                exp.log(
                    "/info GET request failed: non-numeric info_id: {}".format(node_id),
                    key)
                page = error_page(error_type="/info GET, non-numeric info_id")
                js = dumps({"status": "error", "html": page})
                return Response(js, status=403, mimetype='application/json')
        except:
            info_id = None

        # execute the experiment method:
        try:
            infos = exp.info_get_request(participant_id=participant_id, node_id=node_id, info_type=info_type, info_id=info_id)
            session.commit()
        except:
            session.commit()
            print(traceback.format_exc())
            exp.log("info_get_request failed")
            page = error_page(error_type="/info GET, info_get_request error")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

        # parse the data for returning
        exp.log("Creating info data to return", key)
        data = []
        for i in infos:
            data.append({
                "id": i.id,
                "type": i.type,
                "origin_id": i.origin_id,
                "network_id": i.network_id,
                "creation_time": i.creation_time,
                "contents": i.contents,
                "property1": i.property1,
                "property2": i.property2,
                "property3": i.property3,
                "property4": i.property4,
                "property5": i.property5
            })
        data = {"status": "success", "infos": data}

        # return the data
        exp.log("Data successfully created, returning.", key)
        js = dumps(data, default=date_handler)
        return Response(js, status=200, mimetype='application/json')

    elif request.method == "POST":

        # get the contents
        try:
            contents = request.values["contents"]
        except:
            exp.log("/info POST request failed: contents not specified", key)
            page = error_page(error_type="/info POST, contents not specified")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

        # execute the experiment method:
        try:
            info = exp.info_post_request(participant_id=participant_id, node_id=node_id, info_type=info_type, contents=contents)
            session.commit()
        except:
            session.commit()
            print(traceback.format_exc())
            exp.log("info_post_request failed")
            page = error_page(error_type="/info POST, info_post_request error")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

        # parse the data for returning
        exp.log("Creating info data to return", key)
        data = {
            "id": info.id,
            "type": info.type,
            "origin_id": info.origin_id,
            "network_id": info.network_id,
            "creation_time": info.creation_time,
            "contents": info.contents,
            "property1": info.property1,
            "property2": info.property2,
            "property3": info.property3,
            "property4": info.property4,
            "property5": info.property5
        }
        data = {"status": "success", "info": data}

        # return the data
        exp.log("Data successfully created, returning.", key)
        js = dumps(data, default=date_handler)
        return Response(js, status=200, mimetype='application/json')


@custom_code.route("/transmission", methods=["GET", "POST"])
def transmission():
    """ Send GET or POST requests to the transmission table.

    POST requests call the transmission_post_request method
    in Experiment, which, by deafult, prompts one node to
    transmit to another. This request returns a description
    of the new transmission.
    Required arguments: participant_id, node_id
    Optional arguments: destination_id, info_id.

    GET requests call the transmission_get_request method
    in Experiment, which, by default, calls the node's
    transmissions method. This request returns a list of
    descriptions of the transmissions (even if there is only one).
    Required arguments: participant_id, node_id
    Optional arguments: direction, status
    """

    # get the experiment
    exp = experiment(session)

    # get the participant_id
    try:
        participant_id = request.values["participant_id"]
        key = participant_id[0:5]
    except:
        exp.log("/transmission request failed: participant_id not specified")
        page = error_page(error_type="/transmission, participant_id not specified")
        js = dumps({"status": "error", "html": page})
        return Response(js, status=403, mimetype='application/json')

    # get the node_id
    try:
        node_id = request.values["node_id"]
        if not node_id.isdigit():
            exp.log(
                "/transmission request failed: non-numeric node_id: {}"
                .format(node_id), key)
            page = error_page(error_type="/transmission, malformed node_id")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')
    except:
        exp.log("/transmission request failed: node_id not specified", key)
        page = error_page(error_type="/transmission, node_id not specified")
        js = dumps({"status": "error", "html": page})
        return Response(js, status=403, mimetype='application/json')

    if request.method == "GET":
        exp.log("Received a transmission GET request", key)

        # get direction
        try:
            direction = request.values["direction"]
        except:
            direction = "outgoing"

        # get status
        try:
            status = request.values["status"]
        except:
            status = "all"

        # execute the experiment method
        try:
            transmissions = exp.transmission_get_request(
                participant_id=participant_id,
                node_id=node_id,
                direction=direction,
                status=status)
            session.commit()
        except:
            session.commit()
            print(traceback.format_exc())
            exp.log("transmission_get_request failed")
            page = error_page(error_type="/info POST, info_post_request error")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

        # parse the data to return
        exp.log("Creating transmission data to return", key)
        data = []
        for t in transmissions:
            data.append({
                "id": t.id,
                "vector_id": t.vector_id,
                "origin_id": t.origin_id,
                "destination_id": t.destination_id,
                "info_id": t.info_id,
                "network_id": t.network_id,
                "creation_time": t.creation_time,
                "receive_time": t.receive_time,
                "status": t.status,
                "property1": t.property1,
                "property2": t.property2,
                "property3": t.property3,
                "property4": t.property4,
                "property5": t.property5
            })
        data = {"status": "success", "transmissions": data}

        # return the data
        exp.log("Data successfully created, returning.", key)
        js = dumps(data, default=date_handler)
        return Response(js, status=200, mimetype='application/json')

    elif request.method == "POST":
        exp.log("Received a transmission POST request", key)

        # get the info_id
        try:
            info_id = request.values["info_id"]
            if not info_id.isdigit():
                exp.log(
                    "/transmission POST request failed: non-numeric info_id: {}"
                    .format(node_id), key)
                page = error_page(error_type="/transmission POST, non-numeric info_id")
                js = dumps({"status": "error", "html": page})
                return Response(js, status=403, mimetype='application/json')
        except:
            info_id = None

        # get the destination_id
        try:
            destination_id = request.values["destination_id"]
            if not destination_id.isdigit():
                exp.log(
                    "/transmission POST request failed: non-numeric destination_id: {}"
                    .format(node_id), key)
                page = error_page(error_type="/transmission POST, malformed destination_id")
                js = dumps({"status": "error", "html": page})
                return Response(js, status=403, mimetype='application/json')
        except:
            destination_id = None

        # execute the experiment method
        try:
            transmission = exp.transmission_post_request(participant_id=participant_id, node_id=node_id, info_id=info_id, destination_id=destination_id)
            session.commit()
        except:
            session.commit()
            print(traceback.format_exc())
            exp.log("/transmission POST request, transmission_post_request failed.", key)
            page = error_page(error_type="/transmissions POST, transmission_post_request failed")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

        # parse the data for returning
        exp.log("Creating transmission data to return", key)
        data = {
            "id": transmission.id,
            "vector_id": transmission.vector_id,
            "origin_id": transmission.origin_id,
            "destination_id": transmission.destination_id,
            "info_id": transmission.info_id,
            "network_id": transmission.network_id,
            "creation_time": transmission.creation_time,
            "receive_time": transmission.receive_time,
            "status": transmission.status,
            "property1": transmission.property1,
            "property2": transmission.property2,
            "property3": transmission.property3,
            "property4": transmission.property4,
            "property5": transmission.property5
        }
        data = {"status": "success", "transmission": data}

        # return the data
        exp.log("Data successfully created, returning.", key)
        js = dumps(data, default=date_handler)
        return Response(js, status=200, mimetype='application/json')


@custom_code.route("/transformation", methods=["GET", "POST"])
def transformation():
    """ Send GET or POST requests to the transmission table.

    POST requests call the transformation_post_request method
    in Experiment, which, by deafult, creates a new transformation.
    This request returns a description of the new transformation.
    Required arguments: participant_id, node_id, info_in_id, info_out_id
    Optional arguments: type

    GET requests call the transformation_get_request method
    in Experiment, which, by default, calls the node's
    transformations method. This request returns a list of
    descriptions of the transformations (even if there is only one).
    Required arguments: participant_id, node_id
    Optional arguments: transformation_type
    """

    # load the experiment
    exp = experiment(session)

    # get the participant_id
    try:
        participant_id = request.values["participant_id"]
        key = participant_id[0:5]
    except:
        exp.log("/transformation request failed: participant_id not specified")
        page = error_page(error_type="/transformation, participant_id not specified")
        js = dumps({"status": "error", "html": page})
        return Response(js, status=403, mimetype='application/json')
    exp.log("Received a transformation request", key)

    # get the node_id
    try:
        node_id = request.values["node_id"]
        if not node_id.isdigit():
            exp.log(
                "/transformation request failed: non-numeric node_id: {}"
                .format(node_id), key)
            page = error_page(error_type="/transformation, non-numeric node_id")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')
    except:
        exp.log("/transformation request failed: node_id not specified", key)
        page = error_page(error_type="/transformation, node_id not specified")
        js = dumps({"status": "error", "html": page})
        return Response(js, status=403, mimetype='application/json')

    # get the transformation_type
    try:
        transformation_type = request.values["transformation_type"]
        exp.log("transformation_type specified", key)
        if transformation_type in exp.trusted_strings:
            transformation_type = exp.evaluate(transformation_type)
            exp.log("transformation_type in trusted_strings", key)
        else:
            exp.log("/transformation request failed: untrusted transformation_type {}".format(transformation_type), key)
            page = error_page(error_type="/transformation, unstrusted transformation_type")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')
    except:
        transformation_type = models.Transformation
        exp.log("transformation_type not specified, defaulting to Transformation", key)

    if request.method == "GET":

        # execute the experiment method
        try:
            transformations = exp.transformation_get_request(participant_id=participant_id, node_id=node_id, transformation_type=transformation_type)
            session.commit()
        except:
            session.commit()
            print(traceback.format_exc())
            exp.log("/transformation GET request, transformation_get_request failed.", key)
            page = error_page(error_type="/transformation GET, transformation_get_request failed")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

        # parse the data to return
        exp.log("Creating transformation data to return", key)
        data = []
        for t in transformations:
            data.append({
                "id": t.id,
                "info_in_id": t.info_in_id,
                "info_out_id": t.info_out_id,
                "node_id": t.node_id,
                "network_id": t.network_id,
                "creation_time": t.creation_time,
                "property1": t.property1,
                "property2": t.property2,
                "property3": t.property3,
                "property4": t.property4,
                "property5": t.property5
            })
        data = {"status": "success", "transformations": data}

        # return the data
        exp.log("Data successfully created, returning.", key)
        js = dumps(data, default=date_handler)
        return Response(js, status=200, mimetype='application/json')

    if request.method == "POST":

        # get the info_in_id
        try:
            info_in_id = request.values["info_in_id"]
            if not info_in_id.isdigit():
                exp.log(
                    "/transformation request failed: non-numeric info_in_id: {}"
                    .format(info_in_id), key)
                page = error_page(error_type="/transformation, non-numeric info_in_id")
                js = dumps({"status": "error", "html": page})
                return Response(js, status=403, mimetype='application/json')
        except:
            exp.log("/transformation POST request failed: info_in_id not specified", key)
            page = error_page(error_type="/transformation POST, info_in_id not specified")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

        # get the info_out_id
        try:
            info_out_id = request.values["info_out_id"]
            if not info_out_id.isdigit():
                exp.log(
                    "/transformation request failed: non-numeric info_out_id: {}"
                    .format(info_out_id), key)
                page = error_page(error_type="/transformation, non-numeric info_out_id")
                js = dumps({"status": "error", "html": page})
                return Response(js, status=403, mimetype='application/json')
        except:
            exp.log("/transformation POST request failed: info_out_id not specified", key)
            page = error_page(error_type="/transformation POST, info_out_id not specified")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

        # execute the experiment method
        try:
            transformation = exp.transformation_post_request(participant_id=participant_id, node_id=node_id, info_in_id=info_in_id, info_out_id=info_out_id, transformation_type=transformation_type)
            session.commit()
        except:
            session.commit()
            print(traceback.format_exc())
            exp.log("/transformation POST request, transformation_post_request failed.", key)
            page = error_page(error_type="/transformation POST, transformation_post_request failed")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

        # if it returned None return an error
        if transformation is None:
            exp.log("/transformation POST request, transformation_post_request returned None.", key)
            page = error_page(error_type="/transformation POST, transformation_post_request returned None")
            js = dumps({"status": "error", "html": page})
            return Response(js, status=403, mimetype='application/json')

        # parse the data for returning
        exp.log("Transformation successfully posted, creating data to return", key)
        data = {
            "id": transformation.id,
            "type": transformation.type,
            "info_in_id": transformation.info_in_id,
            "info_out_id": transformation.info_out_id,
            "network_id": transformation.network_id,
            "creation_time": transformation.creation_time,
            "property1": transformation.property1,
            "property2": transformation.property2,
            "property3": transformation.property3,
            "property4": transformation.property4,
            "property5": transformation.property5
        }
        data = {"status": "success", "transformation": data}

        # return success
        exp.log("Returning data")
        data = {"status": "success", "transformation": transformation}
        js = dumps(data, default=date_handler)
        return Response(js, status=200, mimetype='application/json')


@custom_code.route("/nudge", methods=["POST"])
def nudge():
    """Call the participant submission trigger for everyone who finished."""
    exp = experiment(session)

    exp.log("Nudging the experiment along.")

    # If a participant is hung at status 4, we must have missed the
    # notification saying they had submitted, so we bump them to status 100
    # and run the submission trigger.
    participants = Participant.query.filter_by(status=4).all()

    for participant in participants:

        exp.log("Nudging participant {}".format(participant))
        participant_id = participant.uniqueid

        # Assign participant status 100.
        participant.status = 100
        session_psiturk.commit()

        # Recruit new participants.
        exp.participant_submission_trigger(
            participant_id=participant_id,
            assignment_id=participant.assignmentid)

    # If a participant has status 3, but has an endhit time, something must
    # have gone awry, so we bump the status to 100 and call it a day.
    participants = Participant.query.filter(
        and_(
            Participant.status == 3,
            Participant.endhit != None)).all()

    for participant in participants:
        exp.log("Bumping {} from status 3 (with endhit time) to 100.")
        participant.status = 100
        session_psiturk.commit()

    return Response(
        dumps({"status": "success"}),
        status=200,
        mimetype='application/json')


@custom_code.route("/notifications", methods=["POST", "GET"])
def api_notifications():
    """Receive MTurk REST notifications."""
    event_type = request.values['Event.1.EventType']
    assignment_id = request.values['Event.1.AssignmentId']

    # Add the notification to the queue.
    q.enqueue(worker_function, event_type, assignment_id, None)

    return Response(
        dumps({"status": "success"}),
        status=200,
        mimetype='application/json')


def check_for_duplicate_assignments(participant):
    participants = Participant.query.filter_by(assignmentid=participant.assignmentid).all()
    duplicates = [p for p in participants if p.uniqueid != participant.uniqueid and p.status < 100]
    for d in duplicates:
        q.enqueue(worker_function, "AssignmentAbandoned", None, d.uniqueid)


def worker_function(event_type, assignment_id, participant_id):
    """Process the notification."""
    exp = experiment(session)
    key = "-----"

    exp.log("Received an {} notification for assignment {}, participant {}".format(event_type, assignment_id, participant_id), key)

    if assignment_id is not None:
        # save the notification to the notification table
        notif = models.Notification(
            assignment_id=assignment_id,
            event_type=event_type)
        session.add(notif)
        session.commit()

        # try to identify the participant
        participants = Participant.query.\
            filter(Participant.assignmentid == assignment_id).\
            all()

        # if there are multiple participants (this is bad news) select the most recent
        if len(participants) > 1:
            participant = max(participants, key=attrgetter('beginhit'))
            exp.log("Warning: Multiple participants associated with this assignment_id. Assuming it concerns the most recent.", key)

        # if there are none (this is also bad news) print an error
        elif len(participants) == 0:
            exp.log("Warning: No participants associated with this assignment_id. Notification will not be processed.", key)
            participant = None

        # if theres only one participant (this is good) select them
        else:
            participant = participants[0]
    elif participant_id is not None:
        participant = Participant.query.filter_by(uniqueid=participant_id).all()[0]
    else:
        participant = None

    if participant is not None:
        participant_id = participant.uniqueid
        key = participant_id[0:5]
        exp.log("Participant identified as {}.".format(participant_id), key)

        if event_type == 'AssignmentAccepted':
            exp.accepted_notification(participant)

        if event_type == 'AssignmentAbandoned':
            if participant.status < 100:
                exp.log("Running abandoned_notification in experiment", key)
                exp.abandoned_notification(participant)
            else:
                exp.log("Participant status > 100 ({}), doing nothing.".format(participant.status), key)

        elif event_type == 'AssignmentReturned':
            if participant.status < 100:
                exp.log("Running returned_notification in experiment", key)
                exp.returned_notification(participant)
            else:
                exp.log("Participant status > 100 ({}), doing nothing.".format(participant.status), key)

        elif event_type == 'AssignmentSubmitted':
            if participant.status < 100:
                exp.log("Running submitted_notification in experiment", key)
                exp.submitted_notification(participant)
            else:
                exp.log("Participant status > 100 ({}), doing nothing.".format(participant.status), key)
        else:
            exp.log("Warning: unknown event_type {}".format(event_type), key)


@custom_code.route('/quitter', methods=['POST'])
def quitter():
    """Overide the psiTurk quitter route."""
    exp = experiment(session)
    exp.log("Quitter route was hit.")
    return Response(
        dumps({"status": "success"}),
        status=200,
        mimetype='application/json')


def error_page(participant=None, error_text=None, compensate=True,
               error_type="default"):
    """Render HTML for error page."""
    if error_text is None:
        if compensate:
            error_text = 'There has been an error and so you are unable to continue, sorry! \
                If possible, please return the assignment so someone else can work on it. \
                Please use the information below to contact us about compensation'
        else:
            error_text = 'There has been an error and so you are unable to continue, sorry! \
                If possible, please return the assignment so someone else can work on it.'

    if participant is not None:
        return render_template(
            'error_wallace.html',
            error_text=error_text,
            compensate=compensate,
            contact_address=config.get('HIT Configuration', 'contact_email_on_error'),
            error_type=error_type,
            hit_id=participant.hitid,
            assignment_id=participant.assignmentid,
            worker_id=participant.workerid
        )
    else:
        return render_template(
            'error_wallace.html',
            error_text=error_text,
            compensate=compensate,
            contact_address=config.get('HIT Configuration', 'contact_email_on_error'),
            error_type=error_type,
            hit_id='unknown',
            assignment_id='unknown',
            worker_id='unknown'
        )


def date_handler(obj):
    return obj.isoformat() if hasattr(obj, 'isoformat') else obj
