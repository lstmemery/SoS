#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

#
# Utility functions used by various executors.
#
import os
import copy
from typing import Any, List, Tuple
from collections import Sequence

from .targets import RemovedTarget, file_target, sos_targets, sos_step, dynamic, sos_variable, RuntimeInfo
from .utils import env
from .eval import SoS_eval, SoS_exec
from ._version import __version__
from .tasks import TaskParams


class PendingTasks(Exception):
    def __init__(self, tasks: List[Tuple[str, str]], *args, **kwargs) -> None:
        super(PendingTasks, self).__init__(*args, **kwargs)
        self.tasks = tasks


def __null_func__(*args, **kwargs) -> Any:
    '''This function will be passed to SoS's namespace and be executed
    to evaluate functions of input, output, and depends directives.'''
    def _flatten(x):
        if isinstance(x, str):
            return [x]
        elif isinstance(x, Sequence):
            return sum((_flatten(k) for k in x), [])
        elif hasattr(x, '__flattenable__'):
            return _flatten(x.flatten())
        else:
            return [x]

    return _flatten(args), kwargs

def clear_output():
    '''
    Remove file targets in `_output` when a step fails to complete
    '''
    for target in env.sos_dict['_output']:
        if isinstance(target, file_target) and target.exists():
            try:
                target.unlink()
                env.logger.warn(f'Removing {target} generated by failed step {env.sos_dict["step_name"]}.')
            except Exception as e:
                env.logger.warning(f'Failed to remove {target}: {e}')

def prepare_env(global_def):
    env.sos_dict.set('__null_func__', __null_func__)
    # initial values
    env.sos_dict.set('SOS_VERSION', __version__)
    try:
        # global def could fail due to execution on remote host...
        # we also execute global_def way before others and allows variables set by
        # global_def be overwritten by other passed variables
        #
        # note that we do not handle parameter in tasks because values should already be
        # in sos_task dictionary
        SoS_exec('''\
import os, sys
from sos.runtime import *
CONFIG = {}
del sos_handle_parameter_
''' + global_def, None)
    except Exception as e:
        env.logger.trace(
            f'Failed to execute global definition {short_repr(global_def)}: {e}')

def create_task(global_def, task_stmt, step_md5):
    # prepare task variables
    env.sos_dict['_runtime']['cur_dir'] = os.getcwd()
    # we need to record the verbosity and sigmode of task during creation because
    # they might be changed while the task is in the queue waiting to be
    # submitted (this happens when tasks are submitted from Jupyter)
    env.sos_dict['_runtime']['verbosity'] = env.verbosity
    env.sos_dict['_runtime']['sig_mode'] = env.config.get(
        'sig_mode', 'default')
    env.sos_dict['_runtime']['run_mode'] = env.config.get(
        'run_mode', 'run')
    env.sos_dict['_runtime']['home_dir'] = os.path.expanduser('~')
    if 'workdir' in env.sos_dict['_runtime'] and not os.path.isdir(os.path.expanduser(env.sos_dict['_runtime']['workdir'])):
        try:
            os.makedirs(os.path.expanduser(
                env.sos_dict['_runtime']['workdir']))
        except Exception:
            raise RuntimeError(
                f'Failed to create workdir {env.sos_dict["_runtime"]["workdir"]}')

    # NOTE: we do not explicitly include 'step_input', 'step_output',
    # 'step_depends' and 'CONFIG'
    # because they will be included by env.sos_dict['__signature_vars__'] if they are actually
    # used in the task. (issue #752)
    task_vars = env.sos_dict.clone_selected_vars(env.sos_dict['__signature_vars__']
                                                 | {'_input', '_output', '_depends', '_index', '__args__', 'step_name', '_runtime',
                                                    '__signature_vars__', '__step_context__'
                                                    })

    task_tags = [env.sos_dict['step_name'], env.sos_dict['workflow_id']]
    if 'tags' in env.sos_dict['_runtime']:
        if isinstance(env.sos_dict['_runtime']['tags'], str):
            tags = [env.sos_dict['_runtime']['tags']]
        elif isinstance(env.sos_dict['_runtime']['tags'], Sequence):
            tags = list(env.sos_dict['_runtime']['tags'])
        else:
            env.logger.warning(
                f'Unacceptable value for parameter tags: {env.sos_dict["_runtime"]["tags"]}')
        #
        for tag in tags:
            if not tag.strip():
                continue
            if not SOS_TAG.match(tag):
                new_tag = re.sub(r'[^\w_.-]', '', tag)
                if new_tag:
                    env.logger.warning(
                        f'Invalid tag "{tag}" is added as "{new_tag}"')
                    task_tags.append(new_tag)
                else:
                    env.logger.warning(f'Invalid tag "{tag}" is ignored')
            else:
                task_tags.append(tag)

    # save task to a file
    task_vars['__task_vars__'] = copy.copy(task_vars)
    taskdef = TaskParams(
        name='{} (index={})'.format(
            env.sos_dict['step_name'], env.sos_dict['_index']),
        global_def=global_def,
        task=task_stmt,          # task
        sos_dict=task_vars,
        tags=task_tags
    )
    # if no output (thus no signature)
    # temporarily create task signature to obtain sig_id
    task_id = RuntimeInfo(step_md5, task_stmt, task_vars['_input'],
                          task_vars['_output'], task_vars['_depends'],
                          task_vars['__signature_vars__'], task_vars).sig_id

    # workflow ID should be included but not part of the signature, this is why it is included
    # after task_id is created.
    task_vars['workflow_id'] = env.sos_dict['workflow_id']
    return task_id, taskdef, task_vars

def reevaluate_output():
    # re-process the output statement to determine output files
    args, _ = SoS_eval(
        f'__null_func__({env.sos_dict["step_output"]._undetermined})')
    if args is True:
        env.logger.error('Failed to resolve unspecified output')
        return
    # handle dynamic args
    args = [x.resolve() if isinstance(x, dynamic) else x for x in args]
    return sos_targets(*args, verify_existence=True)


def validate_step_sig(sig):
    if env.config['sig_mode'] == 'default':
        # if users use sos_run, the "scope" of the step goes beyong names in this step
        # so we cannot save signatures for it.
        if 'sos_run' in env.sos_dict['__signature_vars__']:
            return {}
        else:
            matched = sig.validate()
            if isinstance(matched, dict):
                env.logger.info(
                    f'``{env.sos_dict["step_name"]}`` (index={env.sos_dict["_index"]}) is ``ignored`` due to saved signature')
                return matched
            else:
                env.logger.debug(
                    f'Signature mismatch: {matched}')
                return {}
    elif env.config['sig_mode'] == 'assert':
        matched = sig.validate()
        if isinstance(matched, str):
            raise RuntimeError(
                f'Signature mismatch: {matched}')
        else:
            env.logger.info(
                f'Step ``{env.sos_dict["step_name"]}`` (index={env.sos_dict["_index"]}) is ``ignored`` with matching signature')
            return matched
    elif env.config['sig_mode'] == 'build':
        # build signature require existence of files
        if 'sos_run' in env.sos_dict['__signature_vars__']:
            return {}
        elif sig.write(rebuild=True):
            env.logger.info(
                f'Step ``{env.sos_dict["step_name"]}`` (index={env.sos_dict["_index"]}) is ``ignored`` with signature constructed')
            return {'input': sig.content['input'],
                'output': sig.content['output'],
                'depends': sig.content['depends'],
                'vars': sig.content['end_context']
                }
    elif env.config['sig_mode'] == 'force':
        return {}
    else:
        raise RuntimeError(
            f'Unrecognized signature mode {env.config["sig_mode"]}')


def verify_input(ignore_internal_targets=False):
    # now, if we are actually going to run the script, we
    # need to check the input files actually exists, not just the signatures
    for key in ('_input', '_depends'):
        for target in env.sos_dict[key]:
            if not target.target_exists('target') and not \
                (ignore_internal_targets and isinstance(target, (sos_variable, sos_step))):
                raise RemovedTarget(target)
