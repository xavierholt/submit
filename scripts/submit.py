#!/usr/bin/env python
import base64
import getpass
import hashlib
import json
import os
import requests
import sys
import time
from ConfigParser import ConfigParser
from argparse import ArgumentParser, FileType
from urlparse import urljoin


class Submit(object):
    PATHS = {'auth':           'session',
             'file_item':      'file/{sha1sum}/_',
             'file_item_info': 'file/info/{sha1sum}',
             'project_item':   'class/{class_name}/{project_id}/u/{email}',
             'submission':     'submission'}

    @staticmethod
    def _get_config(section):
        config = ConfigParser()
        if 'APPDATA' in os.environ:  # Windows
            os_config_path = os.environ['APPDATA']
        elif 'XDG_CONFIG_HOME' in os.environ:  # Modern Linux
            os_config_path = os.environ['XDG_CONFIG_HOME']
        elif 'HOME' in os.environ:  # Legacy Linux
            os_config_path = os.path.join(os.environ['HOME'], '.config')
        else:
            os_config_path = None
        locations = [os.path.join(os.path.dirname(__file__), 'submit.ini'),
                     'submit.ini']
        if os_config_path is not None:
            locations.insert(1, os.path.join(os_config_path, 'submit.ini'))
        if not config.read(locations):
            raise Exception('No submit.ini found.')
        if not config.has_section(section) and section != 'DEFAULT':
            raise Exception('No section `{0}` found in submit.ini.'
                            .format(section))
        return dict(config.items(section))

    def __init__(self, config_section):
        config = self._get_config(config_section)
        self._config = config
        self.debug = config['debug'].lower() in ('1', 'true', 'yes')
        self.request_delay = int(config['request_delay'])
        self.request_timeout = int(config['request_timeout'])
        self._url = config['url']
        self.session = requests.session()
        self.session.headers['X-Requested-With'] = 'XMLHttpRequest'
        self.email = None

    def login(self, email=None, password=None):
        """Login to establish a valid session."""
        auth_url = self.url('auth')
        email = email or self._config.get('email')
        password = password or self._config.get('password')
        if password and not email:
            raise Exception('Email must be provided when password is.')
        while True:
            if not email:
                sys.stdout.write('Email: ')
                sys.stdout.flush()
                email = sys.stdin.readline().strip()
                if not email:
                    print('Goodbye!')
                    sys.exit(1)
            if not password:
                password = getpass.getpass('Password for {0}: '.format(email))
            response = self.request(auth_url, 'PUT', email=email,
                                    password=password)
            if response.status_code == 201:
                print('logged in as {0}'.format(email))
                self.email = email
                break
            else:
                print(response.json()['messages'])
                email = password = None

    def msg(self, message):
        """Output a debugging message."""
        if self.debug:
            print('\t' + message)

    def make_submission(self, project, file_mapping):
        _, project_id = project.split(':')
        file_ids = []
        filenames = []
        for file_id, filename in file_mapping:
            file_ids.append(str(file_id))
            filenames.append(filename)
        response = self.request(self.url('submission'), 'PUT',
                                project_id=project_id, file_ids=file_ids,
                                filenames=filenames)
        self.msg('Make submission: {0}'.format(response.status_code))
        if response.status_code == 201:
            url = urljoin(self._url, response.json()['redir_location'])
            print('Submission successful')
            print('Results will be available at: {0}'.format(url))
            return 0
        elif response.status_code == 400:
            print('Submission failed: {0}'.format(response.json()['messages']))
        else:
            print ('Submission failed.')
        return 3

    def request(self, url, method='get', **data):
        time.sleep(self.request_delay)
        args = (json.dumps(data),) if data else ()
        retval = getattr(self.session, method.lower())(
            url, *args, verify=False, timeout=self.request_timeout)
        # Handle outage issues
        if retval.status_code == 502:
            print('The submission site is unexpectedly down. Please email '
                  'bboe@cs.ucsb.edu with the URL: {0}'.format(url))
            sys.exit(1)
        elif retval.status_code == 503:
            print('The submission site is temporarily down for maintenance. '
                  'Please try your submission again in a minute.')
            sys.exit(1)
        return retval

    def send_file(self, the_file):
        print('Sending {0}'.format(os.path.basename(the_file.name)))
        data = the_file.read()
        sha1sum = hashlib.sha1(data).hexdigest()
        test_url = self.url('file_item_info', sha1sum=sha1sum)
        upload_url = self.url('file_item', sha1sum=sha1sum)

        # Have we already uploaded the file?
        response = self.request(test_url, 'GET')
        self.msg('Test file: {0}'.format(response.status_code))
        if response.status_code == 200 and response.json()['owns_file']:
            return response.json()['file_id']
        # Upload the file
        response = self.request(upload_url, 'PUT',
                                b64data=base64.b64encode(data).decode('ascii'))
        self.msg('Send file: {0}'.format(response.status_code))
        if response.status_code == 200:
            return response.json()['file_id']
        else:
            return None

    def submit(self, project, files):
        self.login()
        if not self.verify_access(project):
            print('You cannot submit `{0}`'.format(project))
            return 1
        success = True
        file_mapping = []
        for the_file in files:
            file_id = self.send_file(the_file)
            if file_id is not None:
                filename = os.path.basename(the_file.name)
                file_mapping.append((file_id, filename))
            else:
                success = False
        if not success:
            print('Submission aborted')
            return 2
        return self.make_submission(project, file_mapping)

    def url(self, resource, **kwargs):
        return urljoin(self._url, self.PATHS[resource]).format(**kwargs)

    def verify_access(self, project):
        class_name, project_id = project.split(':')
        url = self.url('project_item',
                       class_name=class_name,
                       project_id=project_id,
                       email=self.email)
        response = self.request(url, 'HEAD')
        self.msg('Access test: {0}'.format(response.status_code))
        return response.status_code in [200, 302]


def main():
    parser = ArgumentParser()
    parser.add_argument('-c', '--config', default='DEFAULT')
    parser.add_argument('-p', '--project', required=True)
    parser.add_argument('files', nargs='+', type=FileType('rb'))
    args = parser.parse_args()

    client = Submit(args.config)
    return client.submit(args.project, args.files)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
