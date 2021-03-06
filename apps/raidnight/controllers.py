import json

from py4web import URL, abort, action, redirect, request
from pydantic import ValidationError

from . import matchmaking, presets, schemas
from .fixtures import auth, db, session, url_signer
from .matchmaking import load_all_signups
from .utils import error, generate_invite_key, get_game_session_full, get_game_signup_full, get_user, \
    is_owner, is_owner_or_signed_up, success


# ==== pages ====
@action("index")
@action.uses("index.html", db, session, auth)
def index():
    user = get_user()
    owned_sessions = None
    signed_up_sessions = None
    sessions = []

    if user is not None:
        owned_sessions = list(db(db.game_sessions.owner_id == user.id).select())
        signed_up_sessions = []

        for signup in db(db.game_signups.user_id == user.id).iterselect():
            game_session = db.game_sessions[signup.session_id]
            if game_session.owner_id != user.id:  # if we own the session, it's already in owned_sessions
                signed_up_sessions.append(game_session)

        sessions = owned_sessions + signed_up_sessions
        sessions = sorted(sessions, key=lambda s: s.id, reverse=True)

    return {
        "user": user,
        "signed_up_sessions": signed_up_sessions,
        "owned_sessions": owned_sessions,
        "sessions": sessions,
    }


@action('sessions/new')
@action.uses("sessions/new.html", db, session, auth)
def create_session():
    return {"user": get_user()}


@action('sessions/<session_id:int>')
@action.uses("sessions/view.html", db, session, auth)
def view_session(session_id):
    user = get_user()
    game_session = get_game_session_full(db, session_id)
    game_signups = load_all_signups(db, session_id)

    if game_session is None:
        abort(404, "Session not found")
    if not is_owner_or_signed_up(game_session, game_signups, user):
        abort(403, "You do not have permission to view this session")

    timespans = matchmaking.find_timespans(game_signups)
    n = 3
    if len(timespans) < n:
        n = len(timespans)
    # optimal times and role assignments
    optimal = []
    for i in range(n):
        time = timespans[i]
        time_start = time[0]
        time_end = time[1]
        signups = time[2]
        solution = matchmaking.RoleSolver(game_session, list(signups)).solve()
        solution_list = [(signup.username, role.name if role else None) for signup, role in
                         solution]
        # return object
        object = {}
        object["time_start"] = time_start
        object["time_end"] = time_end
        object["solution_list"] = solution_list
        optimal.append(object)

    return {
        "user": get_user(),
        "session_id": session_id,
        "game_session": game_session,
        "signups": game_signups,
        "key": game_session.invite_key,
        "optimal": optimal,
        "url_signer": url_signer,
        "json": json
    }


@action('sessions/<session_id:int>/edit')
@action.uses("sessions/edit.html", db, session, auth)
def edit_session(session_id):
    # user must have permission to edit session, session must exist - pretty much everything here is ajax though
    user = get_user()
    game_session = get_game_session_full(db, session_id)
    if game_session is None:
        abort(404, "Session not found")
    if not is_owner(game_session, user):
        abort(403, "You do not have permission to edit this session")
    return {"user": get_user(), "session_id": session_id}


@action('sessions/<session_id:int>/edit_signup/<signup_id:int>')
@action.uses("sessions/edit_signup.html", db, session, auth)
def edit_signup(session_id, signup_id):
    game_session = db.game_sessions[session_id]
    signup = db.game_signups[signup_id]
    user = get_user()
    if game_session is None or signup is None:
        abort(404, "Signup not found")

    # if the signup is from a user, make sure that that user is the current user
    if signup.user_id is not None and user != signup.user_id:
        abort(403, "You are not allowed to edit this user's signups")

    # user is good, serve the page
    return {"user": user, "signup": signup, "session_id": session_id}


@action('sessions/<session_id:int>/delete_signup/<signup_id:int>')
@action.uses(db, session, auth, url_signer.verify())
def delete_signup(session_id, signup_id):
    game_session = db.game_sessions[session_id]
    signup = db.game_signups[signup_id]
    user = get_user()
    if game_session is None or signup is None:
        abort(404, "Signup not found")

    # if the signup is from a user, make sure that that user is the current user
    if not (signup.user_id is None or user == signup.user_id or user == game_session.owner_id):
        abort(403, "You are not allowed to edit this user's signups")

    # delete the signup (cascade to times/roles)
    db(db.game_signups.id == signup_id).delete()

    # if the user is still signed up, redirect them back, otherwise back to index
    if user and db((db.game_signups.session_id == session_id)
                   & (db.game_signups.user_id == user.id)).count():
        redirect(URL('sessions', session_id))
    else:
        redirect(URL('index'))


@action('sessions/<session_id:int>/join', method=['GET', 'POST'])
@action.uses(db, session, auth, url_signer.verify())
def join_session(session_id):
    """after user confirm @ invite - verify join action, create signup, then redir to edit signup"""
    user = get_user()  # may not exist if anonymous - check form for anonymous name
    query = request.forms

    # if user is not logged in and name is not passed to qstring, abort
    if not (user or query.get('name')):
        abort(400, "Expected login or name")

    # get game session
    game_session = db.game_sessions[session_id]
    if game_session is None:
        abort(404, "Game session not found")

    # check if there's already a signup (if so, redir to edit)
    if user:
        existing_signup = db((db.game_signups.session_id == session_id)
                             & (db.game_signups.user_id == user.id)).select().first()
    else:
        existing_signup = db((db.game_signups.session_id == session_id)
                             & (db.game_signups.anonymous_name == query['name'])).select().first()

    if existing_signup is not None:
        redirect(URL(f"sessions/{session_id}/edit_signup/{existing_signup.id}"))

    # create a new signup and redir to edit
    if user:
        new_signup_id = db.game_signups.insert(session_id=session_id, user_id=user.id)
    else:
        new_signup_id = db.game_signups.insert(session_id=session_id, anonymous_name=query['name'])
    redirect(URL(f"sessions/{session_id}/edit_signup/{new_signup_id}"))


@action('invite/<invite_key>')
@action.uses("sessions/invite.html", db, session, auth)
def invite(invite_key):
    user = get_user()
    # get the invite with the corresponding key
    game_invite = db(db.game_invites.key == invite_key).select().first()
    if game_invite is None:
        abort(404, "Invalid invite")
    game_session = db.game_sessions[game_invite.session_id]
    existing_signup = None
    if user:
        existing_signup = db((db.game_signups.session_id == game_invite.session_id)
                             & (db.game_signups.user_id == user.id)).select().first()
    return {
        "user": user,
        "url_signer": url_signer,
        "session": game_session,
        "existing_signup": existing_signup,
        "invite_key": invite_key
    }


@action('sessions/<session_id:int>/delete')
@action.uses(db, session, auth)
def delete_session(session_id=None):
    user = get_user()
    game_session = get_game_session_full(db, session_id)
    if not is_owner(game_session, user):
        abort(403, "You do not have permission to delete this session")
    db(db.game_sessions.id == session_id).delete()

    redirect(URL('index'))


# ==== API ====
@action('api/presets')
def api_get_presets():
    return success([p.to_vue_dict() for p in presets.ALL_PRESETS])


@action('api/sessions', method=['POST'])
@action.uses(db, session, auth)
def api_create_session():
    user = get_user()

    try:
        data = request.json
    except Exception:
        return error(400, "Could not decode JSON")

    # validate the data
    try:
        new_session = schemas.CreateSession.parse_obj(data)
    except ValidationError as e:
        return error(422, str(e))

    # write the data to the database
    # game session
    owner_id = user.id if user is not None else None
    session_id = db.game_sessions.insert(name=new_session.name, description=new_session.description, owner_id=owner_id)

    # roles/rules
    def recursive_insert_role(role, parent_id=None):
        role_id = db.game_roles.insert(session_id=session_id, name=role.name, parent_id=parent_id, icon=role.icon)
        for rule in role.rules:
            db.game_rules.insert(session_id=session_id, role_id=role_id, rule_operator=rule.operator.value,
                                 rule_value=rule.value)
        for child in role.children:
            recursive_insert_role(child, role_id)

    for r in new_session.roles:
        recursive_insert_role(r)

    # create an invite too
    invite_key = generate_invite_key()
    db.game_invites.insert(session_id=session_id, key=invite_key)

    # and a signup for the owner
    if user:
        new_signup_id = db.game_signups.insert(session_id=session_id, user_id=user.id)
    else:
        new_signup_id = None

    return success({"id": session_id, "invite_key": invite_key, "owner_signup_id": new_signup_id})


@action('api/sessions/<session_id:int>', method=['POST'])
@action.uses(db, session, auth)
def api_update_session(session_id=None):
    user = get_user()

    try:
        data = request.json
    except Exception:
        return error(400, "Could not decode JSON")

    # validate the data
    try:
        edited_session = schemas.EditSession.parse_obj(data)
    except ValidationError as e:
        return error(422, str(e))

    game_session = get_game_session_full(db, session_id)
    if not is_owner(game_session, user):
        return error(403, "You do not have permission to edit this session")

    # update database
    # game session
    # update name, description
    db(db.game_sessions.id == session_id).update(name=edited_session.name, description=edited_session.description)
    # update rules
    # first delete all exsiting rules belong to this session
    db(db.game_rules.session_id == session_id).delete()
    # insert new rules
    for rule in edited_session.rules:
        db.game_rules.insert(session_id=session_id, role_id=rule.role_id, rule_operator=rule.operator.value,
                             rule_value=rule.value)

    return success({"id": session_id})


@action('api/sessions/<session_id:int>', method=['GET'])
@action.uses(db, session, auth)
def api_get_session(session_id):
    user = get_user()
    game_session = get_game_session_full(db, session_id)
    game_signups = load_all_signups(db, session_id)
    if game_session is None:
        return error(404, "Session not found")
    if not is_owner_or_signed_up(game_session, game_signups, user):
        return error(403, "You do not have permission to view this session")
    return success(game_session.dict())


@action('api/signups/<signup_id:int>', method=['GET'])
@action.uses(db, session, auth)
def api_get_signup(signup_id):
    user = get_user()
    game_signup = get_game_signup_full(db, signup_id)
    if game_signup is None:
        return error(404, "Signup not found")
    if game_signup.user is not None and user != game_signup.user.id:
        return error(403, "You do not have permission to view this signup")
    return success(game_signup.dict())


@action('api/signups/<signup_id:int>', method=['PUT'])
@action.uses(db, session, auth)
def api_update_signup(signup_id):
    user = get_user()
    existing_signup = db.game_signups[signup_id]
    if existing_signup is None:
        return error(404, "Signup not found")
    if existing_signup.user_id is not None and user != existing_signup.user_id:
        return error(403, "You do not have permission to edit this signup")

    # validate data
    try:
        data = request.json
        new_signup = schemas.EditSignup.parse_obj(data)
    except ValidationError as e:
        return error(422, str(e))
    except Exception:
        return error(400, "Could not decode json")

    # write the data to the database: delete all existing associated rules/times and insert new provided ones
    db(db.game_signup_times.signup_id == signup_id).delete()
    db(db.game_signup_roles.signup_id == signup_id).delete()
    for time in new_signup.times:
        db.game_signup_times.insert(signup_id=signup_id, **time.dict())

    for role in new_signup.roles:
        db.game_signup_roles.insert(signup_id=signup_id, **role.dict())

    return success({'id': signup_id})

# ==== dev test ====
# # todo remove me
# @action('test/vue')
# @action.uses("test/vue2.html", db, session, auth)
# def test_vue():
#     user = get_user()
#     return {"user": user}
#
#
# @action('test/api/vue')
# def test_vue_ajax():
#     return success([{'n': 5}, {'n': 3}, {'n': 1}, {'n': 500}])
#
#
# @action('test/matchmaking/<session_id:int>')
# @action.uses(db)
# def test_matchmaking(session_id):
#     signups = matchmaking.load_all_signups(db, session_id)
#     return success(matchmaking.find_timespans(signups))
#
#
# @action('test/matchmaking2')
# @action.uses(db)
# def test_matchmaking2():
#     signups = get_game_signup_full()
#     session = get_game_session_full(db, 25)
#     solution = matchmaking.RoleSolver(session, signups).solve()
#     solution_list = [{"username": signup.username, "role": role.name if role else None} for signup, role in solution]
#     return success(solution_list)
