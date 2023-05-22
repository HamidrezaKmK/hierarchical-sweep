import typing as th
import re
import dypy as dy
import traceback
from pprint import pprint
import json
import copy

SEPARATOR = "__CUSTOM_SEPERATOR__"
IDX_INDICATOR = "__IDX__"

SWEEP_INDICATION = "sweep"
UNIQUE_NAME_IDENT = "sweep_identifier"
VALUES_DISPLAY_NAME = "sweep_alias"
SWEEP_GROUP = "sweep_group"
UPSERT_GROUP_IDENTIFIER = "upsert"
SWEEP_LIST_OPERATIONS = "sweep_list_operations"
SWEEP_LIST_INSERT = "sweep_insert"
SWEEP_LIST_REMOVE = "sweep_remove"
SWEEP_LIST_OVERWRITE = "sweep_overwrite"
SWEEP_OPERATION_VAL = "dy_eval"

SPLIT = "-"
EXCEPTION_OCCURED = False

SPECIAL_KEYS = [
    SWEEP_INDICATION,
    UNIQUE_NAME_IDENT,
    VALUES_DISPLAY_NAME,
    SWEEP_GROUP,
    SWEEP_LIST_OPERATIONS,
    UPSERT_GROUP_IDENTIFIER,
    SWEEP_OPERATION_VAL,
    SWEEP_LIST_INSERT,
    SWEEP_LIST_REMOVE,
    SWEEP_LIST_OVERWRITE,
]

compression_mapping = {}
value_compression_mapping = {}
remaining_bunch = {}


def compress_parameter_config(parameter_config):
    current_tri = {}

    global compression_mapping
    global value_compression_mapping

    # if unique identifiers are provided in the sweep config, use them
    for key, val in parameter_config.items():
        if isinstance(val, dict):
            inner_dict = val.copy()
            if UNIQUE_NAME_IDENT in inner_dict:
                compression_mapping[key] = inner_dict[UNIQUE_NAME_IDENT]
                inner_dict.pop(UNIQUE_NAME_IDENT)
            if VALUES_DISPLAY_NAME in inner_dict and "values" in inner_dict:
                new_values = []
                for idx, value in enumerate(inner_dict["values"]):
                    if inner_dict[VALUES_DISPLAY_NAME][idx] in value_compression_mapping:
                        raise Exception(
                            f"Value {inner_dict[VALUES_DISPLAY_NAME][idx]} is already used in the sweep config"
                        )
                    value_compression_mapping[inner_dict[VALUES_DISPLAY_NAME][idx]] = value
                    new_values.append(inner_dict[VALUES_DISPLAY_NAME][idx])
                inner_dict["values"] = new_values
                inner_dict.pop(VALUES_DISPLAY_NAME)
            parameter_config[key] = inner_dict

    all_keys = list(parameter_config.keys())
    for key in all_keys:
        to_path = key.split(SEPARATOR)
        # reverse to_path
        from_path = to_path[::-1]
        current_node = current_tri
        current_path = None
        for p in from_path:
            current_path = f"{p}.{current_path}" if current_path is not None else p
            if p not in current_node:
                current_node[p] = {}
                if key not in compression_mapping:
                    compression_mapping[key] = current_path
                break

    ret = {}
    for key, val in parameter_config.items():
        ret[compression_mapping[key]] = val
    return ret


def decompress_parameter_config(parameter_config):
    global compression_mapping
    global value_compression_mapping

    ret = {}
    decompression_mapping = {v: k for k, v in compression_mapping.items()}

    for key, val in parameter_config.items():
        t = val
        if isinstance(t, str) and t in value_compression_mapping:
            t = value_compression_mapping[t]
        ret[decompression_mapping[key]] = t
    return ret


def unflatten_sweep_config(flat_conf: dict):
    conf = {}
    for key, val in flat_conf.items():
        path_to_key = key.split(SEPARATOR)
        cur = conf
        for path in path_to_key[:-1]:
            if path not in cur:
                cur[path] = {}
            cur = cur[path]
        cur[path_to_key[-1]] = val
    return conf


def flatten_sweep_config(tree_conf: dict):
    global compression_mapping
    global value_compression_mapping

    def postprocess_inner_sweep(inner_conf: dict):
        t = inner_conf.copy()
        t.pop(SWEEP_INDICATION)
        return t

    # Define a flattening method for the tree
    def flatten_tree(
        tree_dict: th.Union[dict, th.List]
    ) -> dict:
        ret = {}
        has_something_to_iterate_over = False
        if isinstance(tree_dict, list):
            rem = []
            for idx, val in enumerate(tree_dict):
                if isinstance(val, dict) or isinstance(val, list):
                    if isinstance(val, dict) and SWEEP_INDICATION in val:
                        # pass a version of val without the sweep indication
                        ret[IDX_INDICATOR +
                            str(idx)] = postprocess_inner_sweep(val)
                        has_something_to_iterate_over = True
                    else:
                        flattened, has_something, subrem = flatten_tree(val)
                        if has_something:
                            has_something_to_iterate_over = True
                            for subkey, subval in flattened.items():
                                ret[SEPARATOR.join(
                                    [IDX_INDICATOR + str(idx), subkey])] = subval
                        rem.append(subrem)
                else:
                    rem.append(val)
        elif isinstance(tree_dict, dict):
            rem = {}
            if SWEEP_INDICATION in tree_dict:
                for key, val in tree_dict.items():
                    if key != SWEEP_INDICATION:
                        ret[key] = val
            else:
                for key, val in tree_dict.items():
                    if isinstance(val, dict) or isinstance(val, list):
                        if SWEEP_INDICATION in val:
                            ret[key] = postprocess_inner_sweep(val)
                            has_something_to_iterate_over = True
                        else:
                            flattened, has_something, subrem = flatten_tree(
                                val)
                            if has_something:
                                has_something_to_iterate_over = True
                                for subkey, subval in flattened.items():
                                    ret[SEPARATOR.join([key, subkey])] = subval
                            rem[key] = subrem
                    else:
                        rem[key] = val
        else:
            rem = tree_dict
        return ret, has_something_to_iterate_over, rem

    flattened, _, rem = flatten_tree(tree_conf)
    conf_parameters = {}
    for key, val in flattened.items():
        conf_parameters[key] = val
    return conf_parameters, rem


def sanity_check_special_keys(conf: th.Union[dict, list], current_path: list):
    if isinstance(conf, dict):
        for key, val in conf.items():
            if key in SPECIAL_KEYS:
                raise Exception(
                    f"Key {key} is reserved for sweep configuration and cannot be used in {current_path}"
                )
            if isinstance(val, dict) or isinstance(val, list):
                sanity_check_special_keys(val, current_path + [key])
    elif isinstance(conf, list):
        for idx, val in enumerate(conf):
            if isinstance(val, dict) or isinstance(val, list):
                sanity_check_special_keys(val, current_path + [str(idx)])
# overwrite args recursively


def upsert_config(args: th.Union[th.Dict, th.List],
                  sweep_config: th.Union[th.Dict, th.List, int, float, str],
                  current_path: th.Optional[th.List[str]] = None,
                  root_args: th.Optional[th.Union[th.Dict, th.List]] = None):
    # try and catch an exception and add "line" to the exception and then re-raise it
    if current_path is None:
        current_path = []
    if root_args is None:
        root_args = [x for x in args] if isinstance(
            args, list) else {k: v for k, v in args.items()}

    try:
        if isinstance(args, list):
            if isinstance(sweep_config, dict):
                if SWEEP_LIST_OPERATIONS in sweep_config:
                    ops = sweep_config.pop(SWEEP_LIST_OPERATIONS)
                    if not isinstance(ops, list):
                        raise Exception(
                            f"Expected a list for {SWEEP_LIST_OPERATIONS} but got: {type(ops)}")
                    for op in ops:
                        if len(op.keys()) != 1:
                            raise Exception(
                                "Any sweep list operation should be a dictionary with a single key")
                        if SWEEP_LIST_INSERT in op:
                            val = None
                            idx = op[SWEEP_LIST_INSERT]
                            if not isinstance(idx, int):
                                if isinstance(idx, list):
                                    if len(idx) != 2:
                                        raise Exception(
                                            f"Expected a list of length 2 for {SWEEP_LIST_INSERT} but got: {idx}")
                                    val = idx[1]
                                    idx = idx[0]
                                else:
                                    raise Exception(
                                        f"Expected an integer for {SWEEP_LIST_INSERT} but got: {idx}")
                            if idx == -1:
                                args.append(
                                    sweep_config if val is None else val)
                            else:
                                args.insert(
                                    idx, sweep_config if val is None else val)
                        elif SWEEP_LIST_OVERWRITE in op:
                            val = None
                            idx = op[SWEEP_LIST_OVERWRITE]
                            if not isinstance(idx, int):
                                if isinstance(idx, list):
                                    if len(idx) != 2:
                                        raise Exception(
                                            f"Expected a list of length 2 for {SWEEP_LIST_OVERWRITE} but got: {idx}")

                                    val = idx[1]
                                    idx = idx[0]
                                else:
                                    raise Exception(
                                        f"Expected an integer for {SWEEP_LIST_OVERWRITE} but got: {idx}")
                            if val is None:
                                new_arg = upsert_config(
                                    args[idx], sweep_config, current_path + [str(idx)], root_args)
                                args[idx] = new_arg
                            else:
                                args[idx] = val
                        elif SWEEP_LIST_REMOVE in op:
                            idx = op[SWEEP_LIST_REMOVE]
                            if not isinstance(idx, int):
                                raise Exception(
                                    f"Expected an integer for {SWEEP_LIST_REMOVE} but got: {idx}")
                            args.pop(idx)
                        else:
                            raise Exception(
                                f"Unknown sweep list operation: {op}")
                    sweep_config[SWEEP_LIST_OPERATIONS] = ops
                else:
                    for key, val in sweep_config.items():
                        args_key = int(key[len(IDX_INDICATOR):])
                        if isinstance(val, dict) or isinstance(val, list):

                            if isinstance(val, dict) and SWEEP_OPERATION_VAL in val:
                                if isinstance(val[SWEEP_OPERATION_VAL], str):
                                    args[key] = dy.eval(
                                        val[SWEEP_OPERATION_VAL])(root_args)
                                else:
                                    args[key] = dy.eval(
                                        **val[SWEEP_OPERATION_VAL])(root_args)
                                continue

                            new_args = upsert_config(
                                args[args_key], val, current_path + [str(args_key)], root_args)
                            args[args_key] = new_args

                        elif not isinstance(val, str) or val.find(SWEEP_OPERATION_VAL) == -1:
                            args[args_key] = val
                        else:
                            pat = f"{SWEEP_OPERATION_VAL}\((.*)\)"
                            func_to_eval = re.search(pat, val).group(1)
                            args[args_key] = dy.eval(
                                func_to_eval)(args[args_key])
            elif isinstance(sweep_config, list):
                if len(sweep_config) != len(args):
                    raise Exception(
                        f"Expected a list of length {len(args)} but got a list of length {len(sweep_config)}")
                for idx, val in enumerate(sweep_config):
                    if isinstance(val, dict) or isinstance(val, list):

                        if isinstance(val, dict) and SWEEP_OPERATION_VAL in val:
                            if isinstance(val[SWEEP_OPERATION_VAL], str):
                                args[key] = dy.eval(
                                    val[SWEEP_OPERATION_VAL])(root_args)
                            else:
                                args[key] = dy.eval(
                                    **val[SWEEP_OPERATION_VAL])(root_args)
                            continue

                        new_args = upsert_config(
                            args[idx], val, current_path + [str(idx)], root_args)
                        args[idx] = new_args
                    elif not isinstance(val, str) or val.find(SWEEP_OPERATION_VAL) == -1:
                        args[idx] = val
                    else:
                        pat = f"{SWEEP_OPERATION_VAL}\((.*)\)"
                        func_to_eval = re.search(pat, val).group(1)
                        args[idx] = dy.eval(func_to_eval)(args[idx])

        else:
            all_sweep_group_keys = []
            if isinstance(args, dict):
                is_list_pretender = True
                for key in args.keys():
                    if not key.startswith(IDX_INDICATOR):
                        is_list_pretender = False
                if is_list_pretender:
                    true_args = [None for _ in range(len(args.keys()))]
                    for key in args.keys():
                        true_args[int(key[len(IDX_INDICATOR):])] = args[key]
                    return upsert_config(true_args, sweep_config, current_path, root_args)
                else:
                    all_upsert = []
                    if UPSERT_GROUP_IDENTIFIER in sweep_config:
                        all_upsert = sweep_config.pop(UPSERT_GROUP_IDENTIFIER)
                    for key, val in sweep_config.items():
                        if key.startswith(SWEEP_GROUP):
                            all_sweep_group_keys.append(key)
                            continue
                        if key not in args:
                            args[key] = None
                        if isinstance(val, dict) or isinstance(val, list):

                            if isinstance(val, dict) and SWEEP_OPERATION_VAL in val:
                                if isinstance(val[SWEEP_OPERATION_VAL], str):
                                    args[key] = dy.eval(
                                        val[SWEEP_OPERATION_VAL])(root_args)
                                else:
                                    args[key] = dy.eval(
                                        **val[SWEEP_OPERATION_VAL])(root_args)
                                continue

                            new_args = upsert_config(
                                args[key] if key in args else None, val, current_path + [str(key)], root_args)
                            args[key] = new_args
                        elif not isinstance(val, str) or val.find(SWEEP_OPERATION_VAL) == -1:
                            args[key] = val
                        else:
                            pat = f"{SWEEP_OPERATION_VAL}\((.*)\)"
                            func_to_eval = re.search(pat, val).group(1)
                            args[key] = dy.eval(func_to_eval)(args[key])
            elif isinstance(sweep_config, str) and sweep_config.find(SWEEP_OPERATION_VAL) != -1:
                pat = f"{SWEEP_OPERATION_VAL}\((.*)\)"
                func_to_eval = re.search(pat, sweep_config).group(1)
                return dy.eval(func_to_eval)(args)
            else:
                return sweep_config
            # sort all_sweep_group_keys
            all_sweep_group_keys.sort()
            for key in all_sweep_group_keys:
                val = sweep_config[key]
                new_args = upsert_config(
                    args, val, current_path + [f"{key}"], root_args)
                args = new_args
            for i, val in enumerate(all_upsert):
                new_args = upsert_config(
                    args, val, current_path + [f"{UPSERT_GROUP_IDENTIFIER}-{i}"], root_args)
                args = new_args
        if len(current_path) == 0:
            # Sanity check if any of the nested dicts contain special keys
            # If they do, then we need to throw an error
            # This is because we don't want to allow the user to specify
            # a sweep config that has a special key in it
            sanity_check_special_keys(args, current_path=current_path)

    except Exception as e:
        global EXCEPTION_OCCURED
        if not EXCEPTION_OCCURED:
            EXCEPTION_OCCURED = True
            # update e so that it has the current path
            e.args += ("Configuration path trying to upsert: " +
                       str(current_path),)
            # update e so that it has the sweep_config
            # format sweep_config in a nice string using pprint

            e.args += ("Configuration to upsert: " +
                       json.dumps(sweep_config, indent=2),)
        raise e

    return args


def standardize_sweep_config(sweep_config: dict):
    global remaining_bunch
    config_copy = {k: copy.deepcopy(v) if isinstance(
        v, dict) or isinstance(v, list) else v for k, v in sweep_config.items()}
    flat, remaining_bunch = flatten_sweep_config(config_copy['parameters'])
    config_copy['parameters'] = compress_parameter_config(flat)
    global compression_mapping, value_compression_mapping

    return config_copy, {'keys': compression_mapping, 'values': value_compression_mapping, 'remaining_bunch': remaining_bunch}


def add_where_needed(
    base: th.Union[th.List, th.Dict],
    to_add: th.Union[th.List, th.Dict]
) -> th.Union[th.List, th.Dict]:
    if isinstance(base, list):
        if isinstance(to_add, list):
            for i, val in enumerate(to_add):
                if i >= len(base):
                    raise ValueError(
                        "Cannot add a list to a list where the length of the list to add is greater than the length of the base list")
                else:
                    base[i] = add_where_needed(base[i], val)
        else:
            raise ValueError(
                "Cannot add a non-list to a list where the length of the list to add is greater than the length of the base list"
            )
    elif isinstance(base, dict):
        if isinstance(to_add, dict):
            for key, val in to_add.items():
                if key not in base:
                    base[key] = val
                else:
                    base[key] = add_where_needed(base[key], val)
        else:
            raise ValueError(
                "Cannot add a non-dict to a dict where the length of the dict to add is greater than the length of the base dict"
            )
    return base


def destandardize_sweep_config(
    sweep_config: dict,
    mapping: th.Optional[dict] = None,
) -> dict:
    global compression_mapping, value_compression_mapping, remaining_bunch
    if mapping is not None:
        compression_mapping = mapping['keys']
        value_compression_mapping = mapping['values']
        remaining_bunch = mapping['remaining_bunch']
    config_copy = {k: copy.deepcopy(v) if isinstance(
        v, dict) or isinstance(v, list) else v for k, v in sweep_config.items()}
    config_copy = unflatten_sweep_config(
        decompress_parameter_config(config_copy))
    ret = add_where_needed(config_copy, remaining_bunch)
    return ret
