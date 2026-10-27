"""Serve local assets only for the disposable real-browser rehearsal."""
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.core.wsgi import get_wsgi_application

application = StaticFilesHandler(get_wsgi_application())
