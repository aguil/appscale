#!/usr/bin/env python
""" The AppDashboard is a Google App Engine application that implements a web UI
for interacting with running AppScale deployments. This includes the ability to
create new users, change their authorizations, and upload/remove Google App
Engine applications.
"""
# pylint: disable-msg=F0401
# pylint: disable-msg=C0103
# pylint: disable-msg=E1101
# pylint: disable-msg=W0613

import cgi
import datetime
import jinja2
import logging
import os
import re
import sys
import urllib
import webapp2


try:
  import json
except ImportError:
  import simplejson as json


from google.appengine.api import taskqueue
from google.appengine.ext import ndb
from google.appengine.datastore.datastore_query import Cursor


sys.path.append(os.path.dirname(__file__) + '/lib')
from app_dashboard_helper import AppDashboardHelper
from app_dashboard_helper import AppHelperException
from app_dashboard_data import AppDashboardData
from app_dashboard_data import AppInfo
from app_dashboard_data import RequestInfo


jinja_environment = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__) + \
      os.sep + 'templates'))


class LoggedService(ndb.Model):
  """ A Datastore Model that represents all of the machines running in this
  AppScale deployment.

  Fields:
    hosts: A list of strs, where each str corresponds to the hostname (an IP or
      a FQDN) of a machine running in this AppScale cloud.
  """
  hosts = ndb.StringProperty(repeated=True)


class AppLogLine(ndb.Model):
  """ A Datastore Model that represents a single log line sent by an AppScale
  service.

  Fields:
    message: A str containing the message that was logged.
    level: An integer (typically in the range [1, 5]) corresponding to the
      severity of this log message. Lower log levels correspond to less severe
      log messages.
    timestamp: The time that this log was generated by an AppScale service.
  """
  message = ndb.TextProperty()
  level = ndb.IntegerProperty()
  timestamp = ndb.DateTimeProperty()


class RequestLogLine(ndb.Model):
  """ A Datastore Model that represents all of the logs associated with a single
  web request.

  As not all services within AppScale serve traditional web traffic, a
  RequestLogLine may have only a single AppLogLine.

  Fields:
    service_name: The name of the service that generated this request log. In
      the case of Google App Engine applications, the service name is the
      application's ID.
    host: The hostname of the machine that generated this request log.
    app_logs: One or more AppLogLines that contain logs generated during this
      request.
  """
  service_name = ndb.StringProperty()
  host = ndb.StringProperty()
  app_logs = ndb.StructuredProperty(AppLogLine, repeated=True)


class AppDashboard(webapp2.RequestHandler):
  """ Class that all pages in the Dashboard must inherit from. """


  # Regular expression to capture the continue url.
  CONTINUE_URL_REGEX = 'continue=(.*)$'


  # Regular expression for updating user permissions.
  USER_PERMISSION_REGEX = '^user_permission_'


  # Regular expression that matches email addresses.
  USER_EMAIL_REGEX = '^\w[^@\s]*@[^@\s]{2,}$'


  # The frequency, in seconds, that defines how often Task Queue tasks are fired
  # to update the Dashboard's Datastore cache.
  REFRESH_WAIT_TIME = 10


  def __init__(self, request, response):
    """ Constructor.
    
    Args:
      request: The webapp2.Request object that contains information about the
        current web request.
      response: The webapp2.Response object that contains the response to be
        sent back to the browser.
    """
    self.initialize(request, response)
    self.helper = AppDashboardHelper()
    self.dstore = AppDashboardData(self.helper)


  def render_template(self, template_file, values=None):
    """ Renders a template file with all variables loaded.

    Args: 
      template_file: A str with the relative path to template file.
      values: A dict with key/value pairs used as variables in the jinja
        template files.
    Returns:
      A str with the rendered template.
    """
    if values is None:
      values = {}

    owned_apps = self.dstore.get_owned_apps()
    self.helper.update_cookie_app_list(owned_apps, self.request, self.response)

    template = jinja_environment.get_template(template_file)
    sub_vars = {
      'logged_in' : self.helper.is_user_logged_in(),
      'user_email' : self.helper.get_user_email(),
      'is_user_cloud_admin' : self.dstore.is_user_cloud_admin(),
      'can_upload_apps' : self.dstore.can_upload_apps(),
      'apps_user_is_admin_on' : owned_apps
    }
    for key in values.keys():
      sub_vars[key] = values[key]
    return template.render(sub_vars)


  def get_shared_navigation(self):
    """ Renders the shared navigation.

    Returns:
      A str with the navigation bar rendered.
    """
    return self.render_template(template_file='shared/navigation.html')

  def render_page(self, page, template_file, values=None ):
    """ Renders a template with the main layout and nav bar. """
    if values is None:
      values = {}
    self.response.headers['Content-Type'] = 'text/html'
    template = jinja_environment.get_template('layouts/main.html')
    self.response.out.write(template.render(
        page_name=page,
        page_body=self.render_template(template_file, values),
        shared_navigation=self.get_shared_navigation()
        ))
    

class IndexPage(AppDashboard):
  """ Class to handle requests to the / page. """


  TEMPLATE = 'landing/index.html'


  def get(self):
    """ Handler for GET requests. """
    self.render_page(page='landing', template_file=self.TEMPLATE, values={
      'monitoring_url' : self.dstore.get_monitoring_url(),
    })


class StatusRefreshPage(AppDashboard):
  """ Class to handle requests to the /status/refresh page. """


  def get(self):
    """ Handler for GET requests. Updates all the datastore values with
        information from the AppController and UserAppServer."""
    # Called from taskqueue. Refresh data and display status message.
    self.dstore.update_all()
    self.response.out.write('datastore updated')


  def post(self):
    """ Handler for POST requests. Updates all the datastore values with
        information from the AppController and UserAppServer."""
    # Called from taskqueue. Refresh data and display status message.
    self.dstore.update_all()
    self.response.out.write('datastore updated')


class StatusPage(AppDashboard):
  """ Class to handle requests to the /status page. """


  TEMPLATE = 'status/cloud.html'


  def get(self):
    """ Handler for GET requests. """ 
    # Called from the web.  Refresh data then display page (may be slow).
    if self.request.get('forcerefresh'):
      self.dstore.update_all()

    self.render_page(page='status', template_file=self.TEMPLATE, values={
      'server_info' : self.dstore.get_status_info(),
      'dbinfo' : self.dstore.get_database_info(),
      'service_info' : self.dstore.get_api_status(),
      'apps' : self.dstore.get_application_info(),
      'monitoring_url' : self.dstore.get_monitoring_url(),
    })


class StatusAsJSONPage(webapp2.RequestHandler):
  """ A class that exposes the same information as StatusPage, but via JSON
  instead of raw HTML. """


  def get(self):
    """ Retrieves the cached information about machine-level statistics as a
    JSON-encoded dict. """
    self.response.out.write(json.dumps(AppDashboardData().get_status_info()))


class NewUserPage(AppDashboard):
  """ Class to handle requests to the /users/new and /users/create page. """


  TEMPLATE = 'users/new.html'


  # An int that indicates how many characters passwords must be for new user
  # accounts.
  MIN_PASSWORD_LENGTH = 6


  def parse_new_user_post(self):
    """ Parse the input from the create user form.

    Returns:
      A dict that maps the form fields on the user creation page to None (if
        they pass our validation) or a str indicating why they fail our
        validation.
    """
    users = {}
    error_msgs = {}
    users['email'] = cgi.escape(self.request.get('user_email'))
    if re.match(self.USER_EMAIL_REGEX, users['email']):
      error_msgs['email'] = None
    else:
      error_msgs['email'] = 'Format must be foo@boo.goo.' 

    users['password'] = cgi.escape(self.request.get('user_password'))
    if len(users['password']) >= self.MIN_PASSWORD_LENGTH:
      error_msgs['password'] = None
    else:
      error_msgs['password'] = 'Password must be at least {0} characters ' \
        'long.'.format(self.MIN_PASSWORD_LENGTH)

    users['password_confirmation'] = cgi.escape(
      self.request.get('user_password_confirmation'))
    if users['password_confirmation'] == users['password']:
      error_msgs['password_confirmation'] = None
    else:
      error_msgs['password_confirmation'] = 'Passwords do not match.'

    return error_msgs


  def process_new_user_post(self, errors):
    """ Creates new user if parse was successful.

    Args:
      errors: A dict with True/False values for errors in each of the users
              fields.
    Returns:
      True if user was created, and False otherwise.
    """
    if errors['email'] or errors['password'] or errors['password_confirmation']:
      return False
    else:
      return self.helper.create_new_user(cgi.escape(
           self.request.get('user_email')), cgi.escape(
           self.request.get('user_password')), self.response)


  def post(self):
    """ Handler for POST requests. """
    err_msgs = self.parse_new_user_post()
    try:
      if self.process_new_user_post(err_msgs):
        self.redirect('/', self.response)
        return
    except AppHelperException as err:
      err_msgs['email'] = str(err)
   
    users = {}
    users['email'] = cgi.escape(self.request.get('user_email'))
    users['password'] = cgi.escape(self.request.get('user_password'))
    users['password_confirmation'] = cgi.escape(
      self.request.get('user_password_confirmation'))

    self.render_page(page='users', template_file=self.TEMPLATE, values={
        'user' : users,
        'error_message_content' : err_msgs,
        })


  def get(self):
    """ Handler for GET requests. """
    self.render_page(page='users', template_file=self.TEMPLATE, values={
      #'display_error_messages' : {},
      'user' : {},
      'error_message_content' : {}
    })


class LoginVerify(AppDashboard):
  """ Class to handle requests to /users/confirm and /users/verify pages. """


  TEMPLATE = 'users/confirm.html'


  def post(self):
    """ Handler for POST requests. """
    if self.request.get('continue') != '' and\
       self.request.get('commit') == 'Yes':
      self.redirect(self.request.get('continue').encode('ascii','ignore'), 
        self.response)
    else:
      self.redirect('/', self.response)


  def get(self):
    """ Handler for GET requests. """
    continue_url = urllib.unquote(self.request.get('continue'))
    url_match = re.search(self.CONTINUE_URL_REGEX, continue_url)
    if url_match:
      continue_url = url_match.group(1)

    self.render_page(page='users', template_file=self.TEMPLATE, values={
      'continue' : continue_url
    })


class LogoutPage(AppDashboard):
  """ Class to handle requests to the /users/logout page. """


  def get(self):
    """ Handler for GET requests. Removes the AppScale login cookie and
        redirects the user to the landing page.
    """
    self.helper.logout_user(self.response)
    self.redirect('/', self.response)


class LoginPage(AppDashboard):
  """ Class to handle requests to the /users/login page. """


  TEMPLATE = 'users/login.html'


  def post(self):
    """ Handler for POST requests. """
    if self.helper.login_user(self.request.get('user_email'),
       self.request.get('user_password'), self.response):
    
      if self.request.get('continue') != '':
        self.redirect('/users/confirm?continue={0}'.format(
          urllib.quote(str(self.request.get('continue')))\
          .encode('ascii','ignore')), self.response)
      else:
        self.redirect('/', self.response)
    else:
      self.render_page(page='users', template_file=self.TEMPLATE, values={
          'continue' : self.request.get('continue'),
          'user_email' : self.request.get('user_email'),
          'flash_message': 
          "Incorrect username / password combination. Please try again."
        })


  def get(self):
    """ Handler for GET requests. """
    self.render_page(page='users', template_file=self.TEMPLATE, values={
      'continue' : self.request.get('continue')
    })


class AuthorizePage(AppDashboard):
  """ Class to handle requests to the /authorize page. """


  TEMPLATE = 'authorize/cloud.html'


  def parse_update_user_permissions(self):
    """ Update authorization matrix from form submission.
    
    Returns:
      A str with message to be displayed to the user.
    """
    perms = self.helper.get_all_permission_items()
    req_keys = self.request.POST.keys()
    response = ''
    for fieldname, email in self.request.POST.iteritems():
      if re.match(self.USER_PERMISSION_REGEX, fieldname):
        for perm in perms:
          key = "{0}-{1}".format(email, perm)
          if key in req_keys and \
            self.request.get('CURRENT-{0}'.format(key)) == 'False':
            if self.helper.add_user_permissions(email, perm):
              response += 'Enabling {0} for {1}. '.format(perm, email)
            else:
              response += 'Error enabling {0} for {1}. '.format(perm, email)
          elif key not in req_keys and \
            self.request.get('CURRENT-{0}'.format(key)) == 'True':
            if self.helper.remove_user_permissions(email, perm):
              response += 'Disabling {0} for {1}. '.format(perm, email)
            else:
              response += 'Error disabling {0} for {1}. '.format(perm, email)
    return response


  def post(self):
    """ Handler for POST requests. """
    if self.dstore.is_user_cloud_admin():
      try:
        taskqueue.add(url='/status/refresh')
      except Exception as err:
        logging.exception(err)
      self.render_page(page='authorize', template_file=self.TEMPLATE, values={
        'flash_message' : self.parse_update_user_permissions(),
        'user_perm_list' : self.helper.list_all_users_permissions(),
        })
    else:
      self.render_page(page='authorize', template_file=self.TEMPLATE, values={
        'flash_message':"Only the cloud administrator can change permissions.",
        'user_perm_list':{},
        })


  def get(self):
    """ Handler for GET requests. """
    if self.dstore.is_user_cloud_admin():
      self.render_page(page='authorize', template_file=self.TEMPLATE, values={
        'user_perm_list' : self.helper.list_all_users_permissions(),
      })
    else:
      self.render_page(page='authorize', template_file=self.TEMPLATE, values={
        'flash_message':"Only the cloud administrator can change permissions.",
        'user_perm_list':{},
        })


class AppUploadPage(AppDashboard):
  """ Class to handle requests to the /apps/new page. """


  TEMPLATE = 'apps/new.html'


  def post(self):
    """ Handler for POST requests. """
    success_msg = ''
    err_msg = ''
    if not self.request.POST.multi or \
      'app_file_data' not in self.request.POST.multi or \
      not hasattr(self.request.POST.multi['app_file_data'], 'file'):
      self.render_page(page='apps', template_file=self.TEMPLATE, values={
          'error_message' : 'You must specify a file to upload.',
          'success_message' : ''
        })
      return

    if self.dstore.can_upload_apps():
      try: 
        success_msg = self.helper.upload_app(
          self.request.POST.multi['app_file_data'].file)
      except AppHelperException as err:
        err_msg = str(err)
      if success_msg:
        try:
          taskqueue.add(url='/status/refresh')
          taskqueue.add(url='/status/refresh', countdown=self.REFRESH_WAIT_TIME)
        except Exception as err:
          logging.exception(err)
    else:
      err_msg = "You are not authorized to upload apps."
    self.render_page(page='apps', template_file=self.TEMPLATE, values={
        'error_message' : err_msg,
        'success_message' : success_msg
      })


  def get(self):
    """ Handler for GET requests. """
    self.render_page(page='apps', template_file=self.TEMPLATE)


class AppDeletePage(AppDashboard):
  """ Class to handle requests to the /apps/delete page. """


  TEMPLATE = 'apps/delete.html'


  def get_app_list(self):
    """ Returns a list of apps that the currently logged-in user is an admin of.

    Returns:
      A dict that maps the names of the applications the user is an admin on to
      the URL that the app is hosted at.
    """
    if self.dstore.is_user_cloud_admin():
      return self.dstore.get_application_info()
    else:
      ret_list = {}
      app_list = self.dstore.get_application_info()
      my_apps = self.dstore.get_owned_apps()
      for app in app_list.keys():
        if app in my_apps:
          ret_list[app] = app_list[app]
      return ret_list


  def post(self):
    """ Handler for POST requests. """
    appname = self.request.POST.get('appname')
    if self.dstore.is_user_cloud_admin() or \
       appname in self.dstore.get_owned_apps():
      message = self.helper.delete_app(appname)
      self.dstore.delete_app_from_datastore(appname)
      try:
        taskqueue.add(url='/status/refresh')
        taskqueue.add(url='/status/refresh', countdown=self.REFRESH_WAIT_TIME)
      except Exception as err:
        logging.exception(err)
    else:
      message = "You do not have permission to delete the application: " \
        "{0}".format(appname)
    self.render_page(page='apps', template_file=self.TEMPLATE, values={
      'flash_message' : message,
      'apps' : self.get_app_list(),
      })


  def get(self):
    """ Handler for GET requests. """
    self.render_page(page='apps', template_file=self.TEMPLATE, values={
      'apps' : self.get_app_list(),
    })


class AppsAsJSONPage(webapp2.RequestHandler):
  """ A class that exposes application-level info used on the Cloud Status page,
  but via JSON instead of raw HTML. """


  def get(self):
    """ Retrieves the cached information about applications running in this
    AppScale deployment as a JSON-encoded dict. """
    self.response.out.write(json.dumps(
      AppDashboardData().get_application_info()))


class AppConsole(AppDashboard):
  """ Presents users with an Administrator Console for their Google App Engine
  application, hosted in AppScale. """


  TEMPLATE = 'apps/console.html'


  def get(self, app_id):
    """ Renders the Admin Console for the named application, which currently
    only tells users how many requests are coming to their application per
    second.

    Args:
      app_id: A str that corresponds to the name of the application we should
        create an Admin Console for.
    """
    # Only let the cloud admin and users who own this app see this page.
    is_cloud_admin = self.helper.is_user_cloud_admin()
    apps_user_is_admin_on = self.helper.get_owned_apps()
    if (not is_cloud_admin) and (not apps_user_is_admin_on):
      self.redirect('/', self.response)

    if app_id not in apps_user_is_admin_on:
      self.redirect('/', self.response)

    app_info = AppInfo.get_by_id(app_id)

    self.render_page(page='console', template_file=self.TEMPLATE, values = {
      'app_id' : app_id,
      'requests' : app_info.request_info
    })


  def post(self, app_id):
    """ Saves profiling information about a Google App Engine application to the
    Datastore, for viewing by the GET method.

    Args:
      app_id: A str that uniquely identifies the Google App Engine application
        we are storing data for.
    """
    encoded_data = self.request.body
    data = json.loads(encoded_data)

    app_info = AppInfo.get_by_id(app_id)
    if not app_info:
      app_info = AppInfo(id = app_id, request_info = [])

    request_info = RequestInfo(
      timestamp = datetime.datetime.fromtimestamp(data['timestamp']),
      num_of_requests = data['request_rate'])
    app_info.request_info.append(request_info)
    app_info.put()


class LogMainPage(AppDashboard):
  """ Class to handle requests to the /logs page. """


  TEMPLATE = 'logs/main.html'


  def get(self):
    """ Handler for GET requests. """
    is_cloud_admin = self.helper.is_user_cloud_admin()
    apps_user_is_admin_on = self.helper.get_owned_apps()
    if (not is_cloud_admin) and (not apps_user_is_admin_on):
      self.redirect('/', self.response)

    query = ndb.gql('SELECT * FROM LoggedService')
    all_services = []
    for entity in query:
      if entity.key.id() not in all_services:
        all_services.append(entity.key.id())

    permitted_services = []
    for service in all_services:
      if is_cloud_admin or service in apps_user_is_admin_on:
        permitted_services.append(service)

    self.render_page(page='logs', template_file=self.TEMPLATE, values = {
      'services' : permitted_services
    })


class LogServicePage(AppDashboard):
  """ Class to handle requests to the /logs/service_name page. """


  TEMPLATE = 'logs/service.html'


  def get(self, service_name):
    """ Displays a list of hosts that have logs for the given service. """
    is_cloud_admin = self.helper.is_user_cloud_admin()
    apps_user_is_admin_on = self.helper.get_owned_apps()
    if (not is_cloud_admin) and (service_name not in apps_user_is_admin_on):
      self.redirect('/', self.response)

    service = LoggedService.get_by_id(service_name)
    if service:
      exists = True
      hosts = service.hosts
    else:
      exists = False
      hosts = []

    self.render_page(page='logs', template_file=self.TEMPLATE, values = {
      'exists' : exists,
      'service_name' : service_name,
      'hosts' : hosts
    })


class LogServiceHostPage(AppDashboard):
  """ Class to handle requests to the /logs/service_name/host page. """


  TEMPLATE = 'logs/viewer.html'


  # The number of logs we should present on each page.
  LOGS_PER_PAGE = 20


  def get(self, service_name, host):
    """ Displays all logs accumulated for the given service, on the named host.

    Specifying 'all' as the host indicates that we shouldn't restrict ourselves
    to a single machine.
    """
    is_cloud_admin = self.helper.is_user_cloud_admin()
    apps_user_is_admin_on = self.helper.get_owned_apps()
    if (not is_cloud_admin) and (service_name not in apps_user_is_admin_on):
      self.redirect('/', self.response)

    encoded_cursor = self.request.get('next_cursor')
    if encoded_cursor and encoded_cursor != "None":
      start_cursor = Cursor(urlsafe=encoded_cursor)
    else:
      start_cursor = None

    if host == "all":
      query, next_cursor, is_more = RequestLogLine.query(
        RequestLogLine.service_name == service_name).fetch_page(
        self.LOGS_PER_PAGE, produce_cursors=True, start_cursor=start_cursor)
    else:
      query, next_cursor, is_more = RequestLogLine.query(
        RequestLogLine.service_name == service_name,
        RequestLogLine.host == host).fetch_page(self.LOGS_PER_PAGE,
        produce_cursors=True, start_cursor=start_cursor)

    if next_cursor:
      cursor_value = next_cursor.urlsafe()
    else:
      cursor_value = None

    self.render_page(page='logs', template_file=self.TEMPLATE, values = {
      'service_name' : service_name,
      'host' : host,
      'query' : query,
      'next_cursor' : cursor_value,
      'is_more' : is_more
    })


class LogUploadPage(webapp2.RequestHandler):
  """ Class to handle requests to the /logs/upload page. """


  def post(self):
    """ Saves logs records to the Datastore for later viewing. """
    encoded_data = self.request.body
    data = json.loads(encoded_data)
    service_name = data['service_name']
    host = data['host']
    log_lines = data['logs']

    # First, check to see if this service has been registered.
    service = LoggedService.get_by_id(service_name)
    if service is None:
      service = LoggedService(id = service_name)
      service.hosts = [host]
      service.put()
    else:
      if host not in service.hosts:
        service.hosts.append(host)
        service.put()

    # Next, add in each log line as an AppLogLine
    for log_line_dict in log_lines:
      the_time = int(log_line_dict['timestamp'])
      reversed_time = (2**34 - the_time) * 1000000
      key_name = service_name + host + str(reversed_time)
      log_line = RequestLogLine.get_by_id(id = key_name)
      if not log_line:
        log_line = RequestLogLine(id = key_name)
        log_line.service_name = service_name
        log_line.host = host

      app_log_line = AppLogLine()
      app_log_line.message = log_line_dict['message']
      app_log_line.level = log_line_dict['level']
      app_log_line.timestamp = datetime.datetime.fromtimestamp(the_time)
      log_line.app_logs.append(app_log_line)
      log_line.put()

# Main Dispatcher
app = webapp2.WSGIApplication([ ('/', IndexPage),
                                ('/status/refresh', StatusRefreshPage),
                                ('/status', StatusPage),
                                ('/status/json', StatusAsJSONPage),
                                ('/users/new', NewUserPage),
                                ('/users/create', NewUserPage),
                                ('/logout', LogoutPage),
                                ('/users/logout', LogoutPage),
                                ('/users/login', LoginPage),
                                ('/users/authenticate', LoginPage),
                                ('/login', LoginPage),
                                ('/users/verify', LoginVerify),
                                ('/users/confirm', LoginVerify),
                                ('/authorize', AuthorizePage),
                                ('/apps/new', AppUploadPage),
                                ('/apps/upload', AppUploadPage),
                                ('/apps/delete', AppDeletePage),
                                ('/apps/json', AppsAsJSONPage),
                                ('/apps/(.+)', AppConsole),
                                ('/logs', LogMainPage),
                                ('/logs/upload', LogUploadPage),
                                ('/logs/(.+)/(.+)', LogServiceHostPage),
                                ('/logs/(.+)', LogServicePage)
                              ], debug=True)


def handle_404(_, response, exception):
  """ Handles 404, page not found exceptions. """
  logging.exception(exception)
  response.set_status(404)
  response.write(jinja_environment.get_template('404.html').render())


def handle_500(_, response, exception):
  """ Handles 500, error processing page exceptions. """
  logging.exception(exception)
  response.set_status(500)
  response.write(jinja_environment.get_template('500.html').render())


app.error_handlers[404] = handle_404
app.error_handlers[500] = handle_500
