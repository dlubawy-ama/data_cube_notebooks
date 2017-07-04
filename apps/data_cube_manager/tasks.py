from django.conf import settings
from django.db import connections
from django.forms.models import model_to_dict

from celery.task import task
from celery import chain, group, chord
from celery.utils.log import get_task_logger
from datacube.index import index_connect
from datacube.executor import get_executor
from datacube.config import LocalConfig
from datacube.scripts import ingest

import os
import configparser

from apps.data_cube_manager.models import Dataset, DatasetType, DatasetSource, DatasetLocation, IngestionRequest

logger = get_task_logger(__name__)


@task(name="data_cube_manager.run_ingestion")
def run_ingestion(ingestion_definition):
    conf_path = '/home/' + settings.LOCAL_USER + '/Datacube/data_cube_ui/config/.datacube.conf'
    index = index_connect(local_config=LocalConfig.find([conf_path]))

    source_type, output_type = ingest.make_output_type(index, ingestion_definition)
    ingestion_work.delay(output_type, source_type, ingestion_definition)

    index.close()
    return output_type.id


@task(name="data_cube_manager.ingestion_work")
def ingestion_work(output_type, source_type, ingestion_definition):

    conf_path = '/home/' + settings.LOCAL_USER + '/Datacube/data_cube_ui/config/.datacube.conf'
    index = index_connect(local_config=LocalConfig.find([conf_path]))

    tasks = ingest.create_task_list(index, output_type, None, source_type, ingestion_definition)

    # this is a dry run
    # paths = [ingest.get_filename(ingestion_definition, task['tile_index'], task['tile'].sources) for task in tasks]
    # ingest.check_existing_files(paths)

    # this actually ingests stuff
    successful, failed = ingest.process_tasks(index, ingestion_definition, source_type, output_type, tasks, 3200,
                                              get_executor(None, None))

    index.close()
    return 0


@task(name="data_cube_manager.ingestion_on_demand")
def ingestion_on_demand(ingestion_request_id):
    """
    """

    ingestion_request = IngestionRequest.objects.get(pk=ingestion_request_id)

    cmd = "createdb -U dc_user {}".format(ingestion_request.user)
    os.system(cmd)

    config = get_config(ingestion_request.user)
    index = index_connect(local_config=config, validate_connection=False)
    """conf_path = '/home/' + settings.LOCAL_USER + '/Datacube/data_cube_ui/config/.datacube.conf'
    index = index_connect(local_config=LocalConfig.find([conf_path]))"""

    ingestion_request.update_status("WAIT", "Creating base Data Cube database...")

    ingestion_pipeline = (init_db.si(ingestion_request_id=ingestion_request_id) |
                          add_source_datasets.si(ingestion_request_id=ingestion_request_id) |
                          ingest_subset.si(ingestion_request_id=ingestion_request_id) |
                          prepare_output.si(ingestion_request_id=ingestion_request_id))()

    index.close()


@task(name="data_cube_manager.init_db")
def init_db(ingestion_request_id=None):
    """
    """
    ingestion_request = IngestionRequest.objects.get(pk=ingestion_request_id)

    config = get_config(ingestion_request.user)
    index = index_connect(local_config=config, validate_connection=False)

    index.init_db(with_default_types=True, with_permissions=True)
    index.metadata_types.check_field_indexes(allow_table_lock=False, rebuild_indexes=False, rebuild_views=True)
    index.close()


@task(name="data_cube_manager.add_source_datasets")
def add_source_datasets(ingestion_request_id=None):
    """
    """

    ingestion_request = IngestionRequest.objects.get(pk=ingestion_request_id)
    ingestion_request.update_status("WAIT", "Populating database with source datasets...")

    config = get_config(ingestion_request.user)
    index = index_connect(local_config=config, validate_connection=False)

    dataset_type = DatasetType.objects.using('agdc').get(id=ingestion_request.dataset_type_ref)
    filtering_options = {
        key: getattr(ingestion_request, key)
        for key in [
            'dataset_type_ref', 'start_date', 'end_date', 'latitude_min', 'latitude_max', 'longitude_min',
            'longitude_max'
        ]
    }
    datasets = list(Dataset.filter_datasets(filtering_options))

    dataset_locations = DatasetLocation.objects.using('agdc').filter(dataset_ref__in=datasets)
    dataset_sources = DatasetSource.objects.using('agdc').filter(dataset_ref__in=datasets)

    create_db(ingestion_request.user)

    dataset_type.id = 0
    dataset_type.save(using=ingestion_request.user)

    for dataset in datasets:
        dataset.dataset_type_ref_id = 0

    Dataset.objects.using(ingestion_request.user).bulk_create(datasets)
    DatasetLocation.objects.using(ingestion_request.user).bulk_create(dataset_locations)
    DatasetSource.objects.using(ingestion_request.user).bulk_create(dataset_sources)

    close_db(ingestion_request.user)
    index.close()


@task(name="data_cube_manager.ingest_subset")
def ingest_subset(ingestion_request_id=None):
    """
    """

    ingestion_request = IngestionRequest.objects.get(pk=ingestion_request_id)

    config = get_config(ingestion_request.user)
    index = index_connect(local_config=config, validate_connection=False)

    # Thisis done because of something that the agdc guys do in ingest: https://github.com/opendatacube/datacube-core/blob/develop/datacube/scripts/ingest.py#L168
    ingestion_request.ingestion_definition['filename'] = "ceos_data_cube_sample.yaml"

    source_type, output_type = ingest.make_output_type(index, ingestion_request.ingestion_definition)
    tasks = list(ingest.create_task_list(index, output_type, None, source_type, ingestion_request.ingestion_definition))

    ingestion_request.total_storage_units = len(tasks)
    ingestion_request.update_status("WAIT", "Starting the ingestion process...")

    successful, failed = ingest.process_tasks(index, ingestion_request.ingestion_definition, source_type, output_type,
                                              tasks, 3200, get_executor(None, None))

    index.close()


@task(name="data_cube_manager.prepare_output")
def prepare_output(ingestion_request_id=None):
    """
    """

    ingestion_request = IngestionRequest.objects.get(pk=ingestion_request_id)
    ingestion_request.update_status("WAIT", "Creating output products...")

    config = get_config(ingestion_request.user)
    index = index_connect(local_config=config, validate_connection=False)

    cmd = "pg_dump -U dc_user -n agdc {} > {}".format(ingestion_request.user,
                                                      ingestion_request.get_database_dump_path())
    os.system(cmd)

    index.close()

    cmd = "dropdb -U dc_user {}".format(ingestion_request.user)
    os.system(cmd)


def get_config(username):
    config = configparser.ConfigParser()
    config['datacube'] = {
        'db_password': 'dcuser1',
        'db_connection_timeout': '60',
        'db_username': 'dc_user',
        'db_database': username,
        'db_hostname': settings.MASTER_NODE
    }

    return LocalConfig(config)


def create_db(username):
    connections.databases[username] = {
        'ENGINE': 'django.db.backends.postgresql',
        'OPTIONS': {
            'options': '-c search_path=agdc'
        },
        'NAME': username,
        'USER': 'dc_user',
        'PASSWORD': 'dcuser1',
        'HOST': settings.MASTER_NODE
    }


def close_db(username):
    connections[username].close()
    connections.databases.pop(username)
