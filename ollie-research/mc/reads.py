"""
Read (GET) endpoint handlers for Mission Control.

These were the literal dict bodies of `DashboardHandler.do_GET`. Each handler
takes the live request `handler` and writes the response via its helpers.
Data accessors + the budget helper still live in `research_dashboard` (the
config/state owner), referenced at call time so test patching keeps working.
"""
import research_dashboard as rd
from . import route


@route("GET", "/api/sources")
def get_sources(handler):
    handler._json(200, rd.load_sources())


@route("GET", "/api/interests")
def get_interests(handler):
    handler._json(200, rd.load_interests())


@route("GET", "/api/queue")
def get_queue(handler):
    handler._json(200, rd.load_queue())


@route("GET", "/api/budget")
def get_budget(handler):
    handler._json(200, rd.get_budget_status())
