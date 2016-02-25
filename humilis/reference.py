# -*- coding: utf-8 -*-

"""Built-in reference parsers."""


import contextlib
import os
import importlib
import pip
import subprocess
from subprocess import CalledProcessError
import tempfile
import shutil
from zipfile import ZipFile

import boto3facade
from boto3facade.s3 import S3
from boto3facade.cloudformation import Cloudformation
import jinja2
import keyring

from humilis.exceptions import ReferenceError
import humilis.utils as utils


def _get_s3path(layer, config, full_path):
    """Returns the S3 target (bucket, key) for a local file."""
    env_prefix = "{base_prefix}{env_name}/".format(
        base_prefix=config.profile.get('s3prefix', ''),
        env_name=layer.env_name)
    if layer.env_stage is not None:
        env_prefix = "{}{}/".format(env_prefix, layer.env_stage)

    s3key = "{env_prefix}{layer_name}/{file_name}".format(
        env_prefix=env_prefix,
        layer_name=layer.name,
        file_name=os.path.basename(full_path))
    s3bucket = config.profile.get('bucket')
    return (s3bucket, s3key)


def _git_head():
    """Adds the git HEAD hash to a filename."""
    try:
        c = subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode()
        return c.rstrip()
    except CalledProcessError as err:
        if err.find('Not a git repository') == -1:
            raise


@utils.reference_parser()
def secret(layer, config, service=None, key=None):
    """Retrieves a secret stored in the local keychain.

    :param service: The name of the service the secret applies to
    :param key: The key used to identify the secret within the server

    :returns: The plaintext secret
    """
    return keyring.get_password(service, key)


@utils.reference_parser()
def file(layer, config, path=None):
    """Uploads a local file to S3 and returns the corresponding S3 path.

    :param layer: The Layer object for the layer declaring the reference.
    :param config: An object holding humilis configuration options.
    :param path: Path to the file, relative to the location of meta.yaml.

    :returns: The S3 path where the file has been uploaded.
    """
    full_path = os.path.join(layer.basedir, path)
    s3bucket, s3key = _get_s3path(layer, config, full_path)
    s3 = S3(config)
    s3.cp(full_path, s3bucket, s3key)
    layer.logger.info("{} -> {}/{}".format(full_path, s3bucket, s3key))
    return {'s3bucket': s3bucket, 's3key': s3key}


@utils.reference_parser(name='lambda')
def lambda_ref(layer, config, path=None):
    """Prepares a lambda deployment package and uploads it to S3.

    :param layer: The Layer object for the layer declaring the reference.
    :param config: An object holding humilis configuration options.
    :param path: Path to the file, relative to the location of meta.yaml.

    :returns: S3 path where the deployment package has been uploaded.
    """
    fpath = os.path.abspath(os.path.join(layer.basedir, path))
    logger = layer.logger
    if os.path.isdir(fpath):
        with _deploy_package(fpath, layer, logger) as fpath:
            s3path = file(layer, config, fpath)
    else:
        path_no_ext, ext = os.path.splitext(fpath)
        if ext == '.zip':
            s3path = file(layer, config, fpath)
        else:
            with _simple_deploy_package(fpath, layer, logger) as fpath:
                s3path = file(layer, config, fpath)

    return s3path


@contextlib.contextmanager
def _deploy_package(path, layer, logger):
    """Creates a deployment package for multi-file lambda with deps."""
    with utils.move_aside(path) as tmppath:
        # removes __* and .* dirs
        _cleanup_dir(tmppath)
        _preprocess_dir(tmppath, layer.loader_params)
        setup_file = os.path.join(tmppath, 'setup.py')
        if os.path.isfile(setup_file):
            # Install all depedendencies in the same dir
            pip.main(['install', tmppath, '-t', tmppath])

        # Adding the HEAD hash is needed for CF to detect that the contents of
        # of the .zip file have changed when requesting a stack update.
        gc = _git_head()
        suffix = ('-' + gc, '')[gc is None]
        tmpdir = tempfile.mkdtemp()
        basename = os.path.basename(path)
        zipfile = os.path.join(tmpdir, "{}{}{}".format(basename, suffix,
                                                       '.zip'))
        with ZipFile(zipfile, 'w') as myzip:
            utils.zipdir(tmppath, myzip)
        yield zipfile
        shutil.rmtree(tmpdir)


def _cleanup_dir(path):
    """Removes __* and .* dirs."""
    to_remove = []
    for root, dirs, files in os.walk(path):
        for dirpath in dirs:
            if dirpath.startswith('__') or dirpath.startswith('.'):
                to_remove.append(os.path.join(root, dirpath))
    for dirpath in to_remove:
        shutil.rmtree(dirpath, ignore_errors=True)


@contextlib.contextmanager
def _simple_deploy_package(path, layer, logger):
    """Creates a deployment package for a one-file no-deps lambda."""
    logger.info("Creating deployment package for '{}'".format(path))
    with utils.move_aside(path) as tmppath:
        _preprocess_file(tmppath, layer.loader_params)
        path_no_ext, ext = os.path.splitext(tmppath)
        basename = os.path.basename(path_no_ext)
        gc = _git_head()
        suffix = ('-' + gc, '')[gc is None]
        tmpdir = tempfile.mkdtemp()
        zipfile = os.path.join(tmpdir,
                               "{}{}{}".format(basename, suffix, '.zip'))
        with ZipFile(zipfile, 'w') as myzip:
            myzip.write(tmppath, arcname=basename + ext)
        yield zipfile
        shutil.rmtree(tmpdir)


def _is_jinja2_template(path):
    """Returns true if a file contains a jinja2 template."""
    _, ext = os.path.splitext(path)
    if ext in {'.pyc'}:
        return False

    result = False
    with open(path, 'r') as f:
        for line in f:
            if line.startswith('#') and line.find('preprocessor:jinja2'):
                result = True
                break
    return result


def _preprocess_file(path, params):
    """Renders in place a jinja2 template."""
    if not _is_jinja2_template(path):
        return
    with open(path, 'r') as f:
        result = jinja2.Template(f.read()).render(params)
    with open(path, 'w') as f:
        f.write(result)


def _preprocess_dir(path, params):
    """Preprocesses all files in a directory using Jinja2."""
    for root, dirs, files in os.walk(path):
        for file in files:
            filepath = os.path.join(root, file)
            _preprocess_file(filepath, params)


@utils.reference_parser()
def layer(layer, config, layer_name=None, resource_name=None):
    """Gets the physical ID of a resource in an already deployed layer.

    :param layer: The Layer object for the layer declaring the reference.
    :param config: An object holding humilis configuration options.
    :param layer_name: The name of the layer that contains the target resource.
    :param resource_name: The logical name of the target resource.

    :returns: The physical ID of the resource
    """
    stack_name = utils.get_cf_name(layer.env_name, layer_name,
                                   stage=layer.env_stage)
    cf = Cloudformation(config)
    resource = cf.get_stack_resource(stack_name, resource_name)

    if len(resource) < 1:
        all_stack_resources = [x.logical_resource_id for x
                               in cf.get_stack_resources(stack_name)]
        msg = "{} does not exist in stack {} (with resources {}).".format(
            resource_name, stack_name, all_stack_resources)
        raise ReferenceError(resource_name, msg, logger=layer.logger)

    return resource[0].physical_resource_id


@utils.reference_parser()
def output(layer, config, layer_name=None, output_name=None):
    """Gets the value of an output produced by an already deployed layer.

    :param layer: The Layer object for the layer declaring the reference.
    :param config: An object holding humilis configuration options.
    :param layer_name: The logical name of the layer that produced the output.
    :param output_name: The logical name of the output parameter.
    """
    stack_name = utils.get_cf_name(layer.env_name, layer_name,
                                   stage=layer.env_stage)
    cf = Cloudformation(config)
    output = cf.get_stack_output(stack_name, output_name)
    if len(output) < 1:
        all_stack_outputs = [x['OutputKey'] for x
                             in cf.stack_outputs[stack_name]]
        msg = ("{} output does not exist for stack {} "
               "(with outputs {}).").format(output_name,
                                            stack_name,
                                            all_stack_outputs)
        ref = "output ({}/{})".format(layer_name, output_name)
        raise ReferenceError(ref, msg, logger=layer.logger)
    return output[0]


@utils.reference_parser()
def boto3(layer, config, service=None, call=None, output_attribute=None,
          output_key=None):
    """Calls a boto3facade method.

    :param layer: The Layer object for the layer declaring the reference.
    :param config: An object holding humilis configuration options.
    :param service: The name of the AWS service.
    :param call: A dict with two keys: method, and parameters.
    :param output_attribute: Object attribute to return.
    :param output_key: Dictionary key to return.

    :returns: The call response, or its corresp. attribute or key.
    """
    facade_name = service.title()
    if not hasattr(boto3facade, service):
        ref = "boto3facade.{}.{}.{}: {}".format(service, facade_name,
                                                call['method'],
                                                call['parameters'])
        msg = "Service {} not supported".format(service)
        raise ReferenceError(ref, msg, logger=layer.logger)

    module = importlib.import_module("boto3facade.{}".format(service))
    facade_cls = getattr(module, facade_name)
    facade = facade_cls(config)
    method = getattr(facade, call['method'])
    args = call.get('args', [])
    kwargs = call.get('kwargs', {})
    result = method(*args, **kwargs)
    # If the result is a sequence, we return just the first item
    if hasattr(result, '__iter__'):
        result = list(result)[0]

    if output_attribute is not None:
        return getattr(result, output_attribute)
    elif output_key is not None:
        return result.get(output_key)
    else:
        return result
