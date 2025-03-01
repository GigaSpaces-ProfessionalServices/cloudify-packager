########
# Copyright (c) 2014 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# * See the License for the specific language governing permissions and
# * limitations under the License.

import copy
import stat
import urlparse
import uuid
import json
import os
import time
from socket import gethostbyname
from StringIO import StringIO

import yaml
from fabric.context_managers import settings as fab_env

from cloudify.workflows import local
from test_cli_package import TestCliPackage, HELLO_WORLD_EXAMPLE_NAME, fab, env
from cloudify_cli import constants as cli_constants

FILE_SERVER_PORT = 8080
CHECK_URL = 'www.google.com'


class FabException(Exception):
    """
    Custom exception which replaces the standard SystemExit which is raised
    by fabric on errors.
    """
    pass


class TestOfflineCliPackage(TestCliPackage):
    def _test_cli_package(self):
        """
        Tests cli package in offline mode (Only Linux compatible)
        :return:
        """
        self.keystone_url = self.bootstrap_inputs['keystone_url']
        iaas_resolver = '{0} {1}' \
            .format(*self._resolve_url_to_ip_and_netloc(self.keystone_url))
        iaas_resolver_cmd = 'echo {0} >> /etc/hosts'.format(iaas_resolver)

        # Make sure cli machine is up with a registered ssh key
        wait_for_vm_to_become_ssh_available(env, self._execute_command,
                                            self.logger)

        with self.dns():
            self.logger.info('Preparing CLI and downloading example')
            package_name = self._prepare_cli()
            blueprint_path = self.get_hello_world_blueprint()

        self._assert_offline()
        self._install_cli(package_name)
        self.logger.info('Preparing manager blueprint')
        self.prepare_manager_blueprint()
        self._update_hosts_file(iaas_resolver)

        # Getting the remote manager blueprint and preparing resources
        self.logger.info('Retrieving remote manager blueprint file...')
        manager_blueprint = StringIO()
        fab.get(self.test_manager_blueprint_path, manager_blueprint)
        manager_blueprint_yaml = yaml.load(manager_blueprint.getvalue())
        resources_to_download = self._get_resource_list(manager_blueprint_yaml)

        # each os should implement any vm-related function before this comment

        with FileServer(self._get_file_server_inputs(), resources_to_download,
                        FILE_SERVER_PORT, self.logger) as fs:
            additional_inputs = fs.get_processed_inputs()
            self._update_inputs_file(additional_inputs)

            self.logger.info('Bootstrapping...')
            self.bootstrap_manager()

            # Adding iaas resolver for the manager machine.
            self.logger.info('adding {0} to /etc/hosts of the manager vm'
                             .format(iaas_resolver))
            manager_fab_conf = {
                'user': self.client_user,
                'key_filename': self._get_manager_kp(),
                'host_string': self.manager_ip,
                'timeout': 30,
                'connection_attempts': 10
            }
            wait_for_vm_to_become_ssh_available(manager_fab_conf,
                                                self._execute_command,
                                                self.logger)
            self._run_cmd_on_custom_machine(iaas_resolver_cmd,
                                            manager_fab_conf, sudo=True)

            # Uploading, deploying and testing hello_world blueprint
            # Any sleep is to allow the execution to complete
            # TODO: remove this line when the openstack sg description fix is applied  # NOQA
            self._update_example_sg()

            self.logger.info('Testing the example deployment cycle...')
            blueprint_id = 'blueprint-{0}'.format(uuid.uuid4())

            self._upload_blueprint(blueprint_path, blueprint_id,
                                   self.get_app_blueprint_file())
            self.deployment_id = self.create_deployment(blueprint_id)
            self.addCleanup(self.uninstall_deployment)
            self.install_deployment(self.deployment_id)
            self.assert_deployment_working(
                self._get_app_property('http_endpoint'))

    def _update_hosts_file(self, resolution):
        """
        This function is needed in order to make the transition to windows
        easier
        :param resolution: The resolution rule of url per address.
        :return:
        """
        self._execute_command('echo {0} >> /etc/hosts'.format(resolution),
                              sudo=True)

    def _update_example_sg(self):

        example_blueprint_path = \
            os.path.join("{0}-{1}".format(HELLO_WORLD_EXAMPLE_NAME,
                                          self.branch),
                         self.get_app_blueprint_file())
        example_blueprint = StringIO()
        fab.get(example_blueprint_path, example_blueprint)
        hello_world_blueprint_yaml = yaml.load(example_blueprint.getvalue())

        hello_world_blueprint_yaml['node_templates']['security_group'][
            'properties']['security_group'] = {'description': ''}

        example_blueprint.seek(0)
        json.dump(hello_world_blueprint_yaml, example_blueprint)
        example_blueprint.truncate()

        fab.put(example_blueprint, example_blueprint_path)

    def _assert_offline(self):
        out = self._execute_command('ping -c 2 {0}'.format(CHECK_URL),
                                    warn_only=True)
        self.assertIn('unknown host {0}'.format(CHECK_URL), out)
        self.assertNotIn('bytes from', out)

    def _prepare_cli(self):
        """
        Downloads the cli and any additional resource required by the cli
        :return: cli package name
        """
        self.logger.info('installing cli...')

        self._get_resource(self.cli_package_url, ops='-LO', sudo=True)
        self._get_resource('https://raw.githubusercontent.com/pypa/'
                           'pip/master/contrib/get-pip.py',
                           pipe_command='sudo python2.7 -')
        self._execute_command('pip install virtualenv', sudo=True)

        last_ind = self.cli_package_url.rindex('/')
        return self.cli_package_url[last_ind + 1:]

    def _install_cli(self, package_name):
        """
        Installs the cli from the package_name
        :param package_name: the package to install.
        :return:
        """
        self.logger.info('Installing CLI')
        self._execute_command('rpm -i {0}'.format(package_name), sudo=True)

    def _update_inputs_file(self, additional_inputs):
        """
        Update the remote bootstrap inputs file.

        :param additional_inputs the additional inputs to append
        :return:None
        """
        # Retrieve inputs from cli vm
        inputs_file = StringIO()
        fab.get(self.remote_bootstrap_inputs_path, inputs_file)
        inputs = yaml.load(inputs_file.getvalue())

        # Update the inputs to include the additional inputs
        inputs.update(additional_inputs)
        inputs_file.seek(0)
        json.dump(inputs, inputs_file)
        inputs_file.truncate()

        # Write the inputs back to the cli vm.
        fab.put(inputs_file, self.remote_bootstrap_inputs_path)

    def _get_resource_list(self, blueprint):
        """
        Prepares a list of resources required by the manager blueprint.

        :return: A dict of resources to download
        """
        additional_inputs = {}

        inputs = blueprint.get('inputs')
        if inputs:
            for section in ['agent_package_urls', 'plugin_resources',
                            'dsl_resources']:
                additional_inputs[section] = inputs[section]['default']

            additional_inputs.update(self._get_modules_and_components(inputs))

        return additional_inputs

    def _get_modules_and_components(self, inputs):
        """
        Creates a dictionary of modules and components needed by the manager.
        :param inputs: inputs section of the manager blueprint
        :return: a dict of the cloudfiy modules and external components urls
        """
        resources = {}
        for k, v in inputs.items():
            if urlparse.urlsplit(str(v.get('default', ''))).scheme:
                resources[k] = v['default']
        return resources

    def get_hello_world_blueprint(self):
        """
        Retrieve hello_world blueprint
        :return: the name of the package on the cli vm.
        """
        hello_world_url = self.get_hello_world_url()
        self.logger.info('Downloading hello-world example '
                         'from {0} onto the cli vm'
                         .format(hello_world_url))

        self._get_resource(hello_world_url, ops='-Lk', pipe_command='tar xvz')

        return '{0}-{1}'.format(HELLO_WORLD_EXAMPLE_NAME, self.branch)

    def _upload_blueprint(self, blueprint_path, blueprint_id,
                          blueprint_name):
        """
        Upload blueprint to the vm
        :param blueprint_path: blueprints path.
        :param blueprint_id: blueprints id.
        :param blueprint_name: blueprints main blueprint name.
        :return: None
        """
        self._execute_command('blueprints upload -p {0}/blueprint.yaml'
                              ' -b {1}'.format(blueprint_path,
                                               blueprint_id,
                                               blueprint_name),
                              within_cfy_env=True)

    def _resolve_url_to_ip_and_netloc(self, url):
        """
        Receives an url and translate the netloc part (portless) to an ip, and
        retrieves a tuple with the source netloc and the translated ip
        :param url: the url to resolve
        :return: (original netloc, ip netloc)
        """
        netloc = urlparse.urlsplit(url).netloc
        url_base = netloc.split(':')[0] if ':' in netloc else netloc
        return gethostbyname(url_base), url_base

    def _run_cmd_on_custom_machine(self, cmd, fab_conf, sudo=False, retries=3):
        """
        Runs a command on a remote machine using fabric (would work only on
        machines with fabric).
        :param cmd: the command to run.
        :param fab_conf: fabric environment settings.
        :param sudo: whether run the cmd in sudo mode.
        :param retries: number of times to retry the cmd.
        :return: None
        """
        with fab_env(**fab_conf):
            self._execute_command(cmd, sudo=sudo, retries=retries)

    def _get_manager_kp(self):
        """
        Retrieves manager kp to the local machine.
        :return: path the local manager kp.
        """
        remote_manager_kp_path = self.env.cloudify_config['ssh_key_filename']
        local_manager_kp = fab.get(remote_manager_kp_path, self.workdir)[0]
        os.chmod(local_manager_kp, stat.S_IRUSR | stat.S_IWUSR)
        return local_manager_kp

    def _get_file_server_inputs(self):
        """
        Returns inputs for the file server vm.
        :return: inputs for the file server vm.
        """
        return {
            'prefix': '{0}-FileServer'.format(self.prefix),
            'external_network': self.env.external_network_name,
            'os_username': self.env.keystone_username,
            'os_password': self.env.keystone_password,
            'os_tenant_name': self.env.keystone_tenant_name,
            'os_region': self.env.region,
            'os_auth_url': self.env.keystone_url,
            'image_name': self.env.centos_7_image_id,
            'flavor': self.env.medium_flavor_id,
            'key_pair_path':
                '{0}/{1}-keypair-FileServer.pem'.format(self.workdir,
                                                        self.prefix)
        }


class FileServer(object):
    def __init__(self, inputs, resources, port, logger):
        """
        A class which manager a file server vm.

        :param inputs: the server vm credentials
        :param resources: a dict of resources to put on the filer server.
        :param port: the port for the file server to start on.
        :param logger: for logging purposes only.
        :return:
        """
        self.inputs = inputs
        self.port = port
        self.logger = logger
        self.resources = resources
        self.fileserver_path = 'File-Server'
        self.blueprint = 'test-start-fileserver-vm-blueprint.yaml'
        self.blueprint_path = os.path.join(os.path.dirname(__file__),
                                           'resources', self.blueprint)
        self.server_cmd = 'python -m SimpleHTTPServer {0}'.format(self.port)
        self.fs_base_url = None
        self.local_env = None
        self.processed_inputs = None
        self.fab_env_conf = {}

    def boot(self):
        """
        Boots up the file server vm.
        :return:
        """
        self.logger.info('Initializing file server env')
        self.local_env = local.init_env(self.blueprint_path,
                                        inputs=self.inputs,
                                        name='File-Server',
                                        ignored_modules=cli_constants.
                                        IGNORED_LOCAL_WORKFLOW_MODULES)

        self.logger.info('Starting up a file server vm')
        self.local_env.execute('install', task_retries=40,
                               task_retry_interval=30)

        self.fab_env_conf = {
            'user': 'centos',
            'key_filename': self.inputs['key_pair_path'],
            'host_string': self.local_env.outputs()['vm_public_ip_address'],
            'timeout': 30,
            'connection_attempts': 10,
            'abort_on_prompts': True
        }

        self.fs_base_url = '{0}:{1}'.format(self.fab_env_conf['host_string'],
                                            FILE_SERVER_PORT)

    def process_resources(self):
        """
        A helper method which calls the _process_resources method.
        :return: a list of processed resources.
        """
        self.processed_inputs = self._process_resources(self.resources)
        return self.processed_inputs

    def _process_resources(self, resources):
        """
        Downloads the specified resources and returns the translated resources
        dict.
        :param resources: the resources to download and process.
        :return: a dict of translated resources
        """
        section = {}
        for k, v in resources.items():
            if isinstance(v, dict):
                section[k] = self._process_resources(v)
            elif isinstance(v, list):
                new_list = []
                for entry in v:
                    if isinstance(entry, basestring):
                        new_list.append(self._process_resource(entry))
                    else:
                        new_list.append(self._process_resources(entry))
                section[k] = new_list
            else:
                url_parts = urlparse.urlsplit(v)
                if url_parts.scheme:
                    section[k] = self._process_resource(v)
                else:
                    section[k] = v

        return section

    def _process_resource(self, url):
        """
        Downloads the supplied resource to the file server and returns an url
        on the file server
        :param url: the url of the resource to download
        :return: a new url on the file server.
        """
        url_parts = urlparse.urlsplit(url)
        rel_path = url_parts.path[1:]
        fs_path = os.path.join(self.fileserver_path, rel_path)
        self.logger.info('Downloading {0} to {1}'.format(url, fs_path))
        self._execute_command('curl --create-dirs -Lo {0} {1}'
                              .format(fs_path, url), retries=2)
        url = url.replace(url_parts.netloc, self.fs_base_url)
        url = url.replace(url_parts.scheme, 'http')
        return url

    def teardown(self):
        """
        tears down the file server vm
        :return:
        """
        self.logger.info('Tearing down file server vm')
        self.local_env.execute('uninstall', task_retries=40,
                               task_retry_interval=30)

    def run(self):
        """
        Starts up the file server service on the running vm
        :return:
        """
        # Needed in order to start the file server in detached mode.
        self._execute_command('yum install -y screen', sudo=True, retries=5)

        self.logger.info('Starting up SimpleHTTPServer on {0}'
                         .format(self.port))
        self._execute_command('cd {0} && screen -dm {1}'
                              .format(self.fileserver_path, self.server_cmd),
                              pty=False)

    def stop(self):
        """
        stops the file server service
        :return:
        """
        self.logger.info('Shutting down SimpleHTTPServer')
        stop_cmd = "pkill -9 -f '{0}'".format(self.server_cmd)
        self._execute_command(stop_cmd)

    def get_processed_inputs(self):
        """
        Retrieves the list of translated resources urls.
        :return:
        """
        return self.processed_inputs

    def _execute_command(self, cmd, sudo=False, pty=True, log_cmd=True,
                         retries=0, warn_only=False):
        """
        Executed the given command on the file server vm.

        :param cmd: the command to execute.
        :param sudo: whether to use sudo or not.
        :param pty: passed as an arg to fabric run/sudo.
        :param log_cmd: Specifies whether to log the command executing.
        :param retries: number of command retries.
        :return:
        """
        if log_cmd:
            self.logger.info('Executing command: {0}'.format(cmd))
        else:
            self.logger.info('Executing command: ***')
        with fab_env(**self.fab_env_conf):
            while True:
                if sudo:
                    out = fab.sudo(cmd, pty=pty, warn_only=warn_only)
                else:
                    out = fab.run(cmd, pty=pty, warn_only=warn_only)

                self.logger.info("""Command execution result:
        Status code: {0}
        STDOUT:
        {1}
        STDERR:
        {2}""".format(out.return_code, out, out.stderr))
                if out.succeeded or (warn_only and retries == 0):
                    return out
                else:
                    if retries > 0:
                        time.sleep(30)
                        retries -= 1
                    else:
                        raise Exception('Command: {0} exited with code: '
                                        '{1}. Tried {2} times.'
                                        .format(cmd, out.return_code,
                                                retries + 1))

    def __enter__(self):
        """
        Starts up the file server.
        :return: File server object
        """
        self.boot()
        wait_for_vm_to_become_ssh_available(self.fab_env_conf,
                                            self._execute_command,
                                            self.logger)
        self.process_resources()
        self.run()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Teardown the file server.
        :param exc_type:
        :param exc_val:
        :param exc_tb:
        :return:
        """
        if any([exc_type, exc_val, exc_tb]):
            self.teardown()
        else:
            try:
                self.stop()
            finally:
                self.teardown()
        return False


def wait_for_vm_to_become_ssh_available(env_settings, executor, logger=None,
                                        retries=10, retry_interval=30):
    """
    Asserts that a machine received the ssh key for the key manager, and it is
    no ready to be connected via ssh.
    :param env_settings: The fabric setting for the remote machine.
    :param executor: An executer function, which executes code on the remote
    machine.
    :param logger: custom logger. defaults to None.
    :param retries: number of time to check for availability. default to 5.
    :param retry_interval: length of the intervals between each try. defaults
    to 20.
    :return: None
    """
    local_env_setting = copy.deepcopy(env_settings)
    local_env_setting.update({'abort_exception': FabException})
    if logger:
        logger.info('Waiting for ssh key to register on the vm...')
    while retries >= 0:
        try:
            with fab_env(**local_env_setting):
                executor('echo')
                if logger:
                    logger.info('Machine is ready to be logged in...')
                return
        except FabException as e:
            if retries == 0:
                raise e
            else:
                if logger:
                    logger.info('Machine is not yet ready, waiting for {0} '
                                'secs and trying again'.format(retry_interval))
                retries -= 1
                time.sleep(retry_interval)
                continue
