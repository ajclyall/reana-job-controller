import logging
import time
import pykube
import volume_templates

api = pykube.HTTPClient(pykube.KubeConfig.from_service_account())
api.session.verify = False


def get_jobs():
    return [job.obj for job in pykube.Job.objects(api).
            filter(namespace=pykube.all)]


def add_shared_volume(job, namespace):
    volume = volume_templates.get_k8s_cephfs_volume(namespace)
    mount_path = volume_templates.CEPHFS_MOUNT_PATH
    job['spec']['template']['spec']['containers'][0]['volumeMounts'].append(
        {'name': volume['name'], 'mountPath': mount_path}
    )
    job['spec']['template']['spec']['volumes'].append(volume)


def create_job(job_id, docker_img, cmd, cvmfs_repos, env_vars, namespace,
               shared_file_system):
    job = {
        'kind': 'Job',
        'apiVersion': 'batch/v1',
        'metadata': {
            'name': job_id,
            'namespace': namespace
        },
        'spec': {
            'autoSelector': True,
            'template': {
                'metadata': {
                    'name': job_id
                },
                'spec': {
                    'containers': [
                        {
                            'name': job_id,
                            'image': docker_img,
                            'env': [],
                            'volumeMounts': []
                        },
                    ],
                    'volumes': [],
                    'restartPolicy': 'OnFailure'
                }
            }
        }
    }

    if cmd:
        import shlex
        (job['spec']['template']['spec']['containers']
         [0]['command']) = shlex.split(cmd)

    if env_vars:
        for var, value in env_vars.items():
            job['spec']['template']['spec']['containers'][0]['env'].append(
                {'name': var, 'value': value}
            )

    if shared_file_system:
        add_shared_volume(job, namespace)

    if cvmfs_repos:
        for num, repo in enumerate(cvmfs_repos):
            volume = volume_templates.get_k8s_cvmfs_volume(namespace, repo)
            mount_path = volume_templates.get_cvmfs_mount_point(repo)

            volume['name'] += '-{}'.format(num)
            (job['spec']['template']['spec']['containers'][0]
                ['volumeMounts'].append(
                    {'name': volume['name'], 'mountPath': mount_path}
                ))
            job['spec']['template']['spec']['volumes'].append(volume)

    # add better handling
    try:
        job_obj = pykube.Job(api, job)
        job_obj.create()
        return job_obj
    except pykube.exceptions.HTTPError:
        return None


def watch_jobs(job_db):
    while True:
        logging.debug('Starting a new stream request to watch Jobs')
        stream = pykube.Job.objects(api).filter(namespace=pykube.all).watch()
        for event in stream:
            logging.info('New Job event received')
            job = event.object
            unended_jobs = [j for j in job_db.keys()
                            if not job_db[j]['deleted']]

            if job.name in unended_jobs and event.type == 'DELETED':
                while not job_db[job.name].get('pod'):
                    time.sleep(5)
                    logging.warn(
                        'Job {} Pod still not known'.format(job.name)
                    )
                while job.exists():
                    logging.warn(
                        'Waiting for Job {} to be cleaned'.format(
                            job.name
                        )
                    )
                    time.sleep(5)
                logging.info(
                    'Deleting {}\'s pod -> {}'.format(
                        job.name, job_db[job.name]['pod'].name
                    )
                )
                job_db[job.name]['pod'].delete()
                job_db[job.name]['deleted'] = True

            elif (job.name in unended_jobs and
                  job.obj['status'].get('succeeded')):
                logging.info(
                    'Job {} successfuly ended. Cleaning...'.format(job.name)
                )
                job_db[job.name]['status'] = 'succeeded'
                job.delete()

            # with the current k8s implementation this is never
            # going to happen...
            elif job.name in unended_jobs and job.obj['status'].get('failed'):
                logging.info('Job {} failed. Cleaning...'.format(job.name))
                job_db[job['metadata']['name']]['status'] = 'failed'
                job.delete()


def watch_pods(job_db):
    while True:
        logging.info('Starting a new stream request to watch Pods')
        stream = pykube.Pod.objects(api).filter(namespace=pykube.all).watch()
        for event in stream:
            logging.info('New Pod event received')
            pod = event.object
            unended_jobs = [j for j in job_db.keys()
                            if not job_db[j]['deleted'] and
                            job_db[j]['status'] != 'failed']
            # FIX ME: watch out here, if they change the naming convention at
            # some point the following line won't work. Get job name from API.
            job_name = '-'.join(pod.name.split('-')[:-1])
            # Store existing job pod if not done yet
            if job_name in job_db and not job_db[job_name].get('pod'):
                # Store job's pod
                logging.info(
                    'Storing {} as Job {} Pod'.format(pod.name, job_name)
                )
                job_db[job_name]['pod'] = pod
            # Take note of the related Pod
            if job_name in unended_jobs:
                try:
                    restarts = (pod.obj['status']['containerStatuses'][0]
                                ['restartCount'])
                    exit_code = (pod.obj['status']
                                 ['containerStatuses'][0]
                                 ['state'].get('terminated', {})
                                 .get('exitCode'))
                    logging.info(
                        pod.obj['status']['containerStatuses'][0]['state'].
                        get('terminated', {})
                    )

                    logging.info(
                        'Updating Pod {} restarts to {}'.format(
                            pod.name, restarts
                        )
                    )

                    job_db[job_name]['restart_count'] = restarts

                    if restarts >= job_db[job_name]['max_restart_count'] and \
                       exit_code == 1:

                        logging.info(
                            'Job {} reached max restarts...'.format(job_name)
                        )

                        logging.info(
                            'Getting {} logs'.format(pod.name)
                        )
                        job_db[job_name]['log'] = pod.logs()
                        logging.info(
                            'Cleaning Job {}'.format(job_name)
                        )
                        job_db[job_name]['status'] = 'failed'
                        job_db[job_name]['obj'].delete()

                except KeyError as e:
                    logging.debug('Skipping event because: {}'.format(e))
                    logging.debug(
                        'Event: {}\nObject:\n{}'.format(event.type, pod.obj)
                    )
