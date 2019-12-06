ipykernel_imported = True
try:
    from ipykernel import zmqshell
except ImportError:
    ipykernel_imported = False

import subprocess
import os, logging
import io, yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from os.path import join, dirname


class AlreadyExistError(Exception):
    """Raises when any element(context, cluster) already exists in KUBECONFIG file"""

    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message


class K8sSelection:
    """
    This is the main class for the kernel extension.
    It will be used to handle all the backend tasks.
    """

    def __init__(self, ipython, log):
        self.ipython = ipython
        self.log = log
        self.openstack = 'openstack'
        self.local = 'sa-token'

    def send(self, msg):
        """Send a message to the frontend"""
        self.comm.send(msg)

    def get_auth_type(self, username):
        if username.startswith(self.openstack):
            return self.openstack
        elif username.startswith(self.local):
            return self.local
        else:
            return 'none'

    def handle_comm_message(self, msg):
        """
        Handle message received from frontend.
        There are different actions received from frontend.
        Each action has a specific task to perfrom.
        Every action is handled seperately.
        """
        action = msg['content']['data']['action']
        self.log.info("Received message: %s", str(msg['content']))

        if action == 'Refresh':
            self.cluster_list()
        elif action == 'change-current-context':
            # This action handles the requests from the frontend to change the current context in KUBECONFIG file

            context = msg['content']['data']['context']
            self.log.info(str(msg))
            tab = msg['content']['data']['tab']
            # Logging just for testing purposes
            self.log.info("Changing current context to: ", context)

            try:

                if tab == self.openstack:
                    # Currently unsetting the OS_TOKEN initially everytime while executing the token issue command because
                    # otherwise the command does not work
                    os.environ['OS_TOKEN'] = ''
                    my_env = os.environ.copy()
                    my_env["PYTHONPATH"] = "/usr/local/lib/python3.6/site-packages:" + my_env["PYTHONPATH"]
                    command = ["openstack", "token", "issue", "-c", "id", "-f", "value"]
                    p = subprocess.Popen(command, stdout=subprocess.PIPE, env=my_env)
                    out, err = p.communicate()
                    out = out.decode('utf-8').rstrip('\n')
                    self.log.info("Generated OS_TOKEN: ", out)
                    os.environ['OS_TOKEN'] = out

                # Opening the YAML file using the yaml library
                with io.open(os.environ['HOME'] + '/.kube/config', 'r', encoding='utf8') as stream:
                    load = yaml.safe_load(stream)

                namespace = 'default'
                for i in load['contexts']:
                    if i['name'] == context:
                        if 'namespace' in i['context'].keys():
                            namespace = i['context']['namespace']

                # Creating two empty lists and looping over the contexts and checking whether the clusters are
                # reachable and if the user is admin of the cluster.
                config.load_kube_config(context=context,config_file=os.environ['HOME'] + '/.kube/config')
                api_instance = client.CoreV1Api()
                try:
                    api_response = api_instance.list_namespaced_pod(namespace=namespace, timeout_seconds=2)
                    is_reachable = True
                except:
                    is_reachable = False

                try:
                    api_response = api_instance.list_namespaced_pod(namespace='kube-system', timeout_seconds=2)
                    is_admin = True
                    #if is admin, we check if the spark-services chart has been installed already
                    #if it's not there, we install it
                    create_services_cmd = ["helm", "install", 
                            "--kube-context", context, 
                            "--name", "spark-services",
                            "--set", "cvmfs.enable=true",
                            "https://gitlab.cern.ch/db/spark-service/spark-service-charts/raw/master/cern-spark-services-1.0.0.tgz"]
                    create_user_cmd=["helm", "install", 
                            "--kube-context", context, 
                            "--name", "spark-user-" + os.environ["USER"],
                            "--set", "user.name=" + os.environ["USER"], 
                            "--set", "cvmfs.enable=true", "--set", "user.admin=false",
                            "https://gitlab.cern.ch/db/spark-service/spark-service-charts/raw/spark_user_accounts/cern-spark-user-1.1.0.tgz"]
                    if not self.namespace_exists("spark",api_instance):
                        #if there is no namespace called spark we need to create the chart
                        self.log.info("installing spark-services chart")
                        self.run_helm(create_services_cmd)
                    #disabled atm because of conflict with services
                    #if not self.namespace_exists("spark-"+ os.environ["USER"],api_instance):
                    #    self.log.info("installing spark-user chart")
                    #    self.run_helm(create_user_cmd)

                except Exception as e:
                    self.log.info(e)
                    is_admin = False


                # Setting the current context
                load['current-context'] = context

                # Extracting server IP of the currently selected cluster
                for i in load['contexts']:
                    if i['name'] == load['current-context']:
                        cluster_name = i['context']['cluster']

                for i in load['clusters']:
                    if i['name'] == cluster_name:
                        server_ip = i['cluster']['server']

                # Setting server ip as environment variable
                self.log.info("The current server ip is: ", server_ip)
                os.environ["K8S_MASTER_IP"] = server_ip

                # Writing to the file
                with io.open(os.environ['HOME'] + '/.kube/config', 'w', encoding='utf8') as out:
                    yaml.safe_dump(load, out, default_flow_style=False, allow_unicode=True)

                if is_reachable == True:
                    # Sending the message back to frontend
                    self.send({
                        'msgtype': 'changed-current-context',
                        'is_reachable': is_reachable,
                        'is_admin': is_admin,
                        'context': context
                    })
                else:
                    self.log.info("Context not reachable!")
                    self.send({
                        'msgtype': 'changed-current-context-unsuccessfully',
                        'is_reachable': is_reachable,
                        'is_admin': is_admin,
                        'context': context
                    })
            except Exception as e:
                self.log.info("Failed to kinit or generate os_token")
                self.log.info(str(e))
                error = 'Cannot reach cluster. Please try again after some time.'
                self.send({
                    'msgtype': 'changed-current-context-error',
                    'error': error
                })
        elif action == 'add-context-cluster':
            # This action adds the cluster and context information in the KUBECONFIG file received from the user

            # Here the tab is mode i.e. local, openstack, etc
            tab = msg['content']['data']['tab']

            self.log.info("Adding cluster and context!")

            # We can handle different modes using conditions
            if tab == self.local:
                # Getting all the input data.
                # Note that here we assume that the context name is same as cluster name.
                token = msg['content']['data']['token']
                cluster_name = msg['content']['data']['cluster_name']
                insecure_server = msg['content']['data']['insecure_server']
                ip = msg['content']['data']['ip']
                namespace = "spark-" + str(os.getenv('USER'))
                svcaccount = self.local + '-' + str(os.getenv('USER')) + "-" + cluster_name
                context_name = cluster_name

                # Checking whether user wants an insecure cluster or not
                if insecure_server == "false":
                    catoken = msg['content']['data']['catoken']

                # Setting environment variables to use in bash scripts
                os.environ['SERVICE_ACCOUNT'] = svcaccount
                os.environ['TOKEN'] = token
                os.environ['CONTEXT_NAME'] = context_name
                os.environ['CLUSTER_NAME'] = cluster_name
                os.environ['NAMESPACE'] = namespace
                os.environ['SERVER_IP'] = ip
                if insecure_server == "false":
                    os.environ['CATOKEN'] = catoken

                # The main logic
                try:
                    # Load the KUBECONFIG file
                    with io.open(os.environ['HOME'] + '/.kube/config', 'r', encoding='utf8') as stream:
                        load = yaml.safe_load(stream)

                    contexts = []
                    clusters = []

                    # Get all the context names
                    for i in load['contexts']:
                        contexts.append(i['name'])

                    # Get all the cluster names
                    for i in load['clusters']:
                        clusters.append(i['name'])

                    # Check whether the cluster already exists
                    if cluster_name in clusters:
                        raise AlreadyExistError('Cluster \'{}\' already exist.'.format(cluster_name))

                    # Check whether the context already exists
                    if context_name in contexts:
                        raise AlreadyExistError('Context \'{}\' already exist.'.format(context_name))

                    # Add Cluster to the KUBECONFIG file
                    if insecure_server == "false":
                        load['clusters'].append({
                            'cluster': {
                                'certificate-authority-data': catoken,
                                'server': ip
                            },
                            'name': cluster_name
                        })
                    else:
                        load['clusters'].append({
                            'cluster': {
                                'insecure-skip-tls-verify': True,
                                'server': ip
                            },
                            'name': cluster_name
                        })

                    # Add user to the KUBECONFIG file
                    flag = 0
                    for user in load['users']:
                        if svcaccount == user['name']:
                            if 'token' in user['user'].keys():
                                user['user']['token'] = token
                            flag = 1
                            break

                    if flag == 0:
                        load['users'].append({
                            'user': {
                                'token': token,
                            },
                            'name': svcaccount
                        })

                    # Add Context to the KUBECONFIG file
                    load['contexts'].append({
                        'context': {
                            'cluster': cluster_name,
                            'namespace': namespace,
                            'user': svcaccount
                        },
                        'name': context_name
                    })

                    # Save the file
                    with io.open(os.environ['HOME'] + '/.kube/config', 'w', encoding='utf8') as out:
                        yaml.safe_dump(load, out, default_flow_style=False, allow_unicode=True)

                    self.log.info("Successfully added cluster and context!")
                    self.send({
                        'msgtype': 'added-context-successfully',
                        'tab': self.local
                    })
                except AlreadyExistError as e:
                    # If the context or the cluster already exists then send an error to the user
                    error = e.message
                    self.log.info(str(e))
                    self.send({
                        'msgtype': 'added-context-unsuccessfully',
                        'error': error,
                        'tab': self.local
                    })
                except Exception as e:
                    # Handle general purpose exceptions
                    error = 'Cannot use these settings. Please contact the cluster administrator'
                    self.log.info(str(e))

                    with io.open(os.environ['HOME'] + '/.kube/config', 'r', encoding='utf8') as stream:
                        load = yaml.safe_load(stream)

                    for i in range(len(load['contexts'])):
                        if load['contexts'][i]['name'] == context_name:
                            load['contexts'].pop(i)
                            break

                    for i in range(len(load['clusters'])):
                        if load['clusters'][i]['name'] == cluster_name:
                            load['clusters'].pop(i)
                            break

                    for i in range(len(load['users'])):
                        if load['users'][i]['name'] == svcaccount:
                            load['users'].pop(i)
                            break

                    with io.open(os.environ['HOME'] + '/.kube/config', 'w', encoding='utf8') as out:
                        yaml.safe_dump(load, out, default_flow_style=False, allow_unicode=True)

                    self.send({
                        'msgtype': 'added-context-unsuccessfully',
                        'error': error,
                        'tab': self.local
                    })

            elif tab == self.openstack:
                # Same for the openstack mode. Get the input from the user.
                # The context name is same as cluster name
                cluster_name = msg['content']['data']['cluster_name']
                ip = msg['content']['data']['ip']
                catoken = msg['content']['data']['catoken']
                namespace = "spark-" + str(os.getenv('USER'))
                svcaccount = self.openstack + '-' + str(os.getenv('USER')) + "-" + cluster_name
                context_name = cluster_name

                try:
                    # Check if the KUBECONFIG file is present at default location. If not then create a file
                    if os.path.isdir(os.getenv('HOME') + '/.kube'):
                        if not os.path.isfile(os.getenv('HOME') + '/.kube/config'):
                            load = {}
                            load['apiVersion'] = 'v1'
                            load['clusters'] = []
                            load['contexts'] = []
                            load['current-context'] = ''
                            load['kind'] = 'Config'
                            load['preferences'] = {}
                            load['users'] = []

                            with io.open(os.environ['HOME'] + '/.kube/config', 'w', encoding='utf8') as out:
                                yaml.safe_dump(load, out, default_flow_style=False, allow_unicode=True)
                    else:
                        os.makedirs(os.getenv('HOME') + '/.kube')

                        load = {}
                        load['apiVersion'] = 'v1'
                        load['clusters'] = []
                        load['contexts'] = []
                        load['current-context'] = ''
                        load['kind'] = 'Config'
                        load['preferences'] = {}
                        load['users'] = []

                        with io.open(os.environ['HOME'] + '/.kube/config', 'w', encoding='utf8') as out:
                            yaml.safe_dump(load, out, default_flow_style=False, allow_unicode=True)

                    # Open the KUBECONFIG file
                    with io.open(os.environ['HOME'] + '/.kube/config', 'r', encoding='utf8') as stream:
                        load = yaml.safe_load(stream)

                    contexts = []
                    clusters = []

                    # Get all the cluster names
                    for i in load['clusters']:
                        clusters.append(i['name'])

                    # Get all the context names
                    for i in load['contexts']:
                        contexts.append(i['name'])

                    # Check if the cluster already exists
                    if cluster_name in clusters:
                        raise AlreadyExistError('Cluster \'{}\' already exist.'.format(cluster_name))

                    # Check if the context already exists
                    if context_name in contexts:
                        raise AlreadyExistError('Context \'{}\' already exist.'.format(context_name))

                    # The command which executes when using openstack keystone authentication
                    user_exec_command = {'exec': {'args': ['-c',
                                                           'if [ -z ${OS_TOKEN} ]; then\n    echo \'Error: Missing OpenStack credential from environment variable $OS_TOKEN\' > /dev/stderr\n    exit 1\nelse\n    echo \'{ "apiVersion": "client.authentication.k8s.io/v1alpha1", "kind": "ExecCredential", "status": { "token": "\'"${OS_TOKEN}"\'"}}\'\nfi\n'],
                                                  'command': '/bin/bash',
                                                  'apiVersion': 'client.authentication.k8s.io/v1alpha1'}}

                    # Add cluster to the KUBECONFIG file
                    load['clusters'].append({
                        'cluster': {
                            'certificate-authority-data': catoken,
                            'server': ip
                        },
                        'name': cluster_name
                    })

                    # Add context to the KUBECONFIG file
                    load['contexts'].append({
                        'context': {
                            'cluster': cluster_name,
                            'namespace': namespace,
                            'user': svcaccount
                        },
                        'name': context_name
                    })

                    # Add user to the KUBECONFIG file
                    flag = 0
                    for i in load['users']:
                        if i['name'] == svcaccount:
                            flag = 1
                            break

                    if flag == 0:
                        load['users'].append({
                            'user': user_exec_command,
                            'name': svcaccount
                        })

                    with io.open(os.environ['HOME'] + '/.kube/config', 'w', encoding='utf8') as out:
                        yaml.safe_dump(load, out, default_flow_style=False, allow_unicode=True)

                    self.log.info("Successfully added cluster and context!")
                    self.send({
                        'msgtype': 'added-context-successfully',
                        'tab': self.openstack
                    })
                except AlreadyExistError as e:
                    # If the context or cluster already exists then send the error to the user.
                    error = e.message
                    self.log.info(str(e))
                    self.send({
                        'msgtype': 'added-context-unsuccessfully',
                        'error': error,
                        'tab': self.openstack
                    })
                except Exception as e:
                    # Handle general purpose exceptions.
                    error = 'Cannot use these settings. Please contact the cluster administrator'
                    self.log.info(str(e))

                    with io.open(os.environ['HOME'] + '/.kube/config', 'r', encoding='utf8') as stream:
                        load = yaml.safe_load(stream)

                    for i in range(len(load['contexts'])):
                        if load['contexts'][i]['name'] == context_name:
                            load['contexts'].pop(i)
                            break

                    for i in range(len(load['clusters'])):
                        if load['clusters'][i]['name'] == cluster_name:
                            load['clusters'].pop(i)
                            break

                    for i in range(len(load['users'])):
                        if load['users'][i]['name'] == svcaccount:
                            load['users'].pop(i)
                            break

                    with io.open(os.environ['HOME'] + '/.kube/config', 'w', encoding='utf8') as out:
                        yaml.safe_dump(load, out, default_flow_style=False, allow_unicode=True)

                    self.send({
                        'msgtype': 'added-context-unsuccessfully',
                        'error': error,
                        'tab': self.openstack
                    })
        elif action == "delete-current-context":
            #TODO this does not work now, it leaves unused cluster and user in the file
            self.log.info("Deleting context from KUBECONFIG")
            # This action deletes the context and cluster from the KUBECONFIG file
            context = msg['content']['data']['context']

            try:
                # Open the file
                with io.open(os.environ['HOME'] + '/.kube/config', 'r', encoding='utf8') as stream:
                    load = yaml.safe_load(stream)

                # Get the user to delete
                for i in range(len(load['contexts'])):
                    if load['contexts'][i]['name'] == context:
                        user = load['contexts'][i]['context']['user']
                        break

                # Delete the given cluster and context
                for i in range(len(load['contexts'])):
                    if load['contexts'][i]['name'] == context:
                        load['contexts'].pop(i)
                        break

                for i in range(len(load['clusters'])):
                    if load['clusters'][i]['name'] == context:
                        load['clusters'].pop(i)
                        break

                #Delete the user
                for i in range(len(load['users'])):
                    if load['users'][i]['name'] == user:
                        load['users'].pop(i)
                        break

                # If the current context is deleted, also change the current-context in the kubeconfig file
                current_context_deleted = False
                if context == load['current-context']:
                    load['current-context'] = ''
                    current_context_deleted = True

                current_context = load['current-context']

                # Save the file
                with io.open(os.environ['HOME'] + '/.kube/config', 'w', encoding='utf8') as out:
                    yaml.safe_dump(load, out, default_flow_style=False, allow_unicode=True)

                self.log.info("Successfully deleted context")
                self.send({
                    'msgtype': 'deleted-context-successfully',
                    'current_context': current_context,
                    'current_context_deleted': current_context_deleted
                })
            except Exception as e:
                # Handle general exception
                error = "Cannot open KUBECONFIG file"
                self.log.info(str(e))
                self.send({
                    'msgtype': 'deleted-context-unsuccessfully',
                })
        elif action == "create-user":
            self.log.info("Creating User!")

            # This action adds a remote user to the cluster. Only admins can perform this action.
            error = ''
            username = msg['content']['data']['username']
            email = msg['content']['data']['email']
            selected_context = msg['content']['data']['context']

            # Declaring the naming conventions of the resources to be created or checked
            namespace = 'spark-' + username
            rolebinding_name = 'edit-cluster-' + namespace

            try:
                # Load the KUBECONFIG file
                with io.open(os.environ['HOME'] + '/.kube/config', 'r', encoding='utf8') as stream:
                    load = yaml.safe_load(stream)

                for i in load['contexts']:
                    if i['name'] == selected_context:
                        selected_cluster = i['context']['cluster']
                        break

                if not self.namespace_exists("spark-"+ username,api_instance):
                    create_user_cmd=["helm", "install", "--name", "spark-user-" + username, "--set",
                                "user.name=" + username, "--set", "cvmfs.enable=true", "--set", "user.admin=false",
                                "https://gitlab.cern.ch/db/spark-service/spark-service-charts/raw/spark_user_accounts/cern-spark-user-1.1.0.tgz"]

                    out = self.run_helm(create_user_cmd)


                    # If helm chart is deployed successfully, send message to frontend
                    if out.decode('utf-8') != '':
                        # Get the server ip of the cluster to be sent in the email to the user.
                        for i in load['clusters']:
                            if i['name'] == selected_cluster:
                                server_ip = i['cluster']['server']
                                ca_cert = i['cluster']['certificate-authority-data']
                                break


                        self.log.info("Successfully created user")
                        self.send({
                            'msgtype': 'added-user-successfully',
                            'ca_cert': ca_cert,
                            'server_ip': server_ip,
                            'cluster_name': selected_cluster
                        })
                else:
                    error = 'Cannot create user due to some error.'
                    self.send({
                        'msgtype': 'added-user-unsuccessfully',
                        'error': error
                    })
            except Exception as e:
                # Handle user creation exceptions
                error = 'Cannot create user due to some error.'
                self.log.info(str(e))
                self.send({
                    'msgtype': 'added-user-unsuccessfully',
                    'error': error
                })
        elif action == 'kerberos-auth':
            self.log.info("Getting Kerberos ticket and os_token!")

            try:
                auth_kinit = msg['content']['data']['password']
                p = subprocess.Popen(['kinit', os.getenv("USER") + '@CERN.CH'], stdin=subprocess.PIPE,
                                     universal_newlines=True)
                p.communicate(input=auth_kinit)

                if p.wait() == 0:
                    self.log.info("Got kerberos ticket and os_token successfully!")
                    self.send({
                        'msgtype': 'auth-successfull',
                    })
                else:
                    error = 'Error obtaining the ticket. Is the password correct?'
                    self.send({
                        'msgtype': 'auth-unsuccessfull',
                        'error': error
                    })
            except Exception as e:
                error = 'Error executing the commands'
                self.log.info(str(e))
                self.send({
                    'msgtype': 'auth-unsuccessfull',
                    'error': error
                })
        elif action == 'check-auth-required':
            context = msg['content']['data']['context']

            try:
                with io.open(os.environ['HOME'] + '/.kube/config', 'r', encoding='utf8') as stream:
                    load = yaml.safe_load(stream)

                namespace = 'default'
                for i in load['contexts']:
                    if i['name'] == context:
                        if 'namespace' in i['context'].keys():
                            namespace = i['context']['namespace']

                config.load_kube_config(context=context)
                api_instance = client.CoreV1Api()
                api_instance.list_namespaced_pod(namespace=namespace, timeout_seconds=2)

                self.send({
                    'msgtype': 'auth-not-required'
                })
            except Exception as e:
                self.log.info(str(e))
                self.send({
                    'msgtype': 'auth-required'
                })

    def send_sendgrid_email(self, dotenv_path, email, selected_cluster, ca_cert, server_ip):
        """
        If the admin has sendgrid API credentials, then they can use this function to send email
        :param dotenv_path: path of sendgrid.env file
        :param email: email of the receiver
        :param selected_cluster: the name of cluster that we want to send the info of
        :param ca_cert: ca_cert of the cluster
        :param server_ip: ip of the cluster
        :return:
        """

        self.log.info("Sending email using sendgrid!")
        try:
            from dotenv import load_dotenv
            from sendgrid import SendGridAPIClient, SendGridException
            from sendgrid.helpers.mail import Mail, To, From

            # Load Sendgrid API key
            load_dotenv(dotenv_path)

            # Create an email message.
            # The 'from_email' is currently hardcoded. It will have to be changed later.
            message = Mail(
                from_email=From('sahil.jajodia@gmail.com'),
                to_emails=To(email),
                subject='Credentials for cluster: ' + selected_cluster,
                html_content='<strong>Cluster name: </strong>' + selected_cluster + '<br><br><strong>CA Cert: </strong>' + ca_cert + '<br><br><strong>Server IP: </strong>' + server_ip)

            # Send the email to the user.
            sg = SendGridAPIClient(os.environ.get('SENDGRID_API_KEY'))
            response = sg.send(message)
            self.log.info("Successfully sent email using sendgrid!")
        except ImportError as e:
            # Handle import exceptions
            error = 'Cannot send email.'
            self.log.info(str(e))
            self.send({
                'msgtype': 'added-user-unsuccessfully',
                'error': error
            })
        except SendGridException as e:
            # Handle sendgrid exceptions
            error = 'Cannot send email.'
            self.log.info(str(e))
            self.send({
                'msgtype': 'added-user-unsuccessfully',
                'error': error
            })
        except Exception as e:
            error = 'Cannot send email.'
            self.log.info(str(e))
            self.send({
                'msgtype': 'added-user-unsuccessfully',
                'error': error
            })

    def send_email(self, email, selected_cluster, ca_cert, server_ip):
        """
        This function can be used to send emails from an internal account at CERN
        :param email: email of the receiver
        :param selected_cluster: the name of cluster that we want to send the info of
        :param ca_cert: ca_cert of the cluster
        :param server_ip: ip of the cluster
        :return:
        """

        self.log.info("Sending email!")
        try:
            from email.mime.text import MIMEText

            body = '''
                Cluster name: {selected_cluster}\n\nCA Cert: {ca_cert}\n\nServer IP: {server_ip} 
            '''

            # Sending the mail
            body = body.format(selected_cluster=selected_cluster, ca_cert=ca_cert, server_ip=server_ip)
            msg = MIMEText(body)
            msg["From"] = os.environ["USER"] + "@cern.ch"
            msg["To"] = email
            msg["Subject"] = "Credentials for cluster: " + selected_cluster
            p = subprocess.Popen(["/usr/sbin/sendmail", "-t", "-oi"], stdin=subprocess.PIPE)
            p.communicate(msg.as_bytes())
            self.log.info("Successfully sent email")
        except Exception as e:
            # Handle email exceptions
            error = 'Cannot send email.'
            self.log.info(str(e))
            self.send({
                'msgtype': 'added-user-unsuccessfully',
                'error': error
            })

    def register_comm(self):
        """ Register a comm_target which will be used by frontend to start communication """
        self.ipython.kernel.comm_manager.register_target(
            "K8sSelection", self.target_func)

    def target_func(self, comm, msg):
        """ Callback function to be called when a frontend comm is opened """
        self.log.info("Established connection to frontend")
        self.log.debug("Received message: %s", str(msg))
        self.comm = comm

        @self.comm.on_msg
        def _recv(msg):
            self.handle_comm_message(msg)

        #KUBECONFIG initially points somewhere. in this function it is set to os.getenv('HOME') + '/.kube/config'
        self.merge_service_into_user()
        self.cluster_list()

    def merge_service_into_user(self):
        with open(os.getenv("KUBECONFIG")) as f:
            service_kubeconf=yaml.safe_load(f)
        os.environ["KUBECONFIG"]=os.getenv('HOME') + '/.kube/config'

        self.create_empty_kconfig_if_needed(os.environ["KUBECONFIG"])
        with open(os.environ["KUBECONFIG"]) as f:
            existing_kubeconf=yaml.safe_load(f)

        def add_ifnotthere(key, kubeconf):
            if (service_kubeconf[key][0] in kubeconf[key]):
                self.log.info(f"not merging service cluster in user kubeconfig because {key} already there")
            else:
                kubeconf[key].append(service_kubeconf[key][0])
        
        add_ifnotthere('contexts',existing_kubeconf)
        add_ifnotthere('users',existing_kubeconf)
        add_ifnotthere('clusters',existing_kubeconf)
        
        with open(os.environ["KUBECONFIG"], 'w') as f:
            yaml.safe_dump(existing_kubeconf, f)
        
    def create_empty_kconfig_if_needed(self, kpath):
        emptyload = {}
        emptyload['apiVersion'] = 'v1'
        emptyload['clusters'] = []
        emptyload['contexts'] = []
        emptyload['current-context'] = ''
        emptyload['kind'] = 'Config'
        emptyload['preferences'] = {}
        emptyload['users'] = []

        if os.path.isdir(os.path.dirname(kpath)):
            if (not os.path.isfile(kpath)
                or os.stat(kpath).st_size < 8 ):

                with io.open(kpath, 'w', encoding='utf8') as out:
                    yaml.safe_dump(emptyload, out, default_flow_style=False, allow_unicode=True)
        else:
            os.makedirs(os.path.dirname(kpath))

            with io.open(kpath, 'w', encoding='utf8') as out:
                yaml.safe_dump(emptyload, out, default_flow_style=False, allow_unicode=True)


    def cluster_list(self):
        """
        Get the list of contexts and clusters from the KUBECONFIG file and send it to the frontend
        """

        self.log.info("Getting clusters and contexts from KUBECONFIG")

        try:


            with io.open(os.environ["KUBECONFIG"], 'r', encoding='utf8') as stream:
                load = yaml.safe_load(stream)

            contexts = load['contexts']
            active_context = ''
            for i in range(len(contexts)):
                if contexts[i]['name'] == load['current-context']:
                    active_context = contexts[i]
                    break

            # Getting the type of authentication used by contexts
            cluster_auth_type = {}
            current_cluster_auth_type = ''
            for i in range(len(contexts)):
                auth_type = self.get_auth_type(contexts[i]['context']['user'])
                if load['current-context'] != '' and contexts[i]['name'] == load['current-context']:
                    current_cluster_auth_type = auth_type
                cluster_auth_type[contexts[i]['name']]=auth_type

            contexts={context['name']:cluster_auth_type[context['name']] for context in load['contexts']}
            #is this used anywhere????
            clusters = [cluster['name'] for cluster in load['clusters']]

            current_context = ''
            if active_context is not '':
                current_context = active_context['name']

            current_cluster = ''
            for i in load['contexts']:
                if i['name'] == load['current-context']:
                    current_cluster = i['context']['cluster']

            self.send({
                'msgtype': 'context-select',
                'contexts': contexts,
                'active_context': current_context,
                'clusters': clusters,
                'current_cluster': current_cluster,
                'cluster_auth_type': cluster_auth_type,
                'current_cluster_auth_type': current_cluster_auth_type
            })
        except Exception as e:
            error = "Error getting cluster list. The Kubeconfig file is probably corrupted. You can delete it using 'rm $HOME/.kube/config' on the terminal."
            self.log.info(str(e))
            self.send({
                'msgtype': 'get-clusters-unsuccessfull',
                'error': error
            })

    def namespace_exists(self, ns, api_instance):
        return ns in (ns.metadata.name  for ns in api_instance.list_namespace().items)

    def run_helm(self,command_to_run):
        command = ["helm", "init", "--client-only"]
        p = subprocess.Popen(command, stdout=subprocess.PIPE)
        out, err = p.communicate()
        # If helm is initialized successfully then deploy te helm chart
        if out.decode('utf-8') != '':
            my_env = os.environ.copy()
            p = subprocess.Popen(command_to_run, stdout=subprocess.PIPE, env=my_env)
            out, err = p.communicate()

        return out

def load_ipython_extension(ipython):
    """ Load Jupyter kernel extension """

    log = logging.getLogger('tornado.k8sselection.kernelextension')
    log.name = 'k8sselection.kernelextension'
    log.setLevel(logging.INFO)
    log.propagate = True

    if ipykernel_imported:
        if not isinstance(ipython, zmqshell.ZMQInteractiveShell):
            log.error("K8sSelection: Ipython not running through notebook. So exiting.")
            return
    else:
        return

    log.info("Starting K8sSelection Kernel Extension")
    ext = K8sSelection(ipython, log)
    ext.register_comm()