import argparse
import enum
from typing import List, Optional, Tuple
from torchbenchmark.util.backends import list_backends, BACKENDS

from torchbenchmark.util.backends.flops import enable_fvcore_flops
from torchbenchmark.util.backends.fx2trt import enable_fx2trt
from torchbenchmark.util.backends.torch_trt import enable_torchtrt

TEST_STAGE = enum.Enum('TEST_STAGE', ['FORWARD', 'BACKWARD', 'OPTIMIZER', 'ALL'])

def check_correctness_p(
    model: 'torchbenchmark.util.model.BenchmarkModel',
    opt_args: argparse.Namespace,
    dargs: argparse.Namespace,
) -> bool:
    "If correctness check should be enabled."
    # if the model doesn't support correctness check (like detectron2), skip it
    if hasattr(model, 'SKIP_CORRECTNESS_CHECK') and model.SKIP_CORRECTNESS_CHECK:
        return False
    if dargs.skip_correctness:
        return False
    is_eval_test = model.test == "eval"
    # always check correctness with torchdynamo
    if model.dynamo:
        return True
    opt_args_dict = vars(opt_args)
    for k in opt_args_dict:
        if opt_args_dict[k]:
            return is_eval_test
    return False

def add_bool_arg(parser: argparse.ArgumentParser, name: str, default_value: bool=True):
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument('--' + name, dest=name, action='store_true')
    group.add_argument('--no-' + name, dest=name, action='store_false')
    parser.set_defaults(**{name: default_value})

def is_timm_model(model: 'torchbenchmark.util.model.BenchmarkModel') -> bool:
    return hasattr(model, 'TIMM_MODEL') and model.TIMM_MODEL

def is_torchvision_model(model: 'torchbenchmark.util.model.BenchmarkModel') -> bool:
    return hasattr(model, 'TORCHVISION_MODEL') and model.TORCHVISION_MODEL

def is_hf_model(model: 'torchbenchmark.util.model.BenchmarkModel') -> bool:
    return hasattr(model, 'HF_MODEL') and model.HF_MODEL

def is_fambench_model(model: 'torchbenchmark.util.model.BenchmarkModel') -> bool:
    return hasattr(model, 'FAMBENCH_MODEL') and model.FAMBENCH_MODEL

def is_staged_train_test(model: 'torchbenchmark.util.model.BenchmarkModel') -> bool:
    return hasattr(model, 'forward') and hasattr(model, 'backward') and hasattr(model, 'optimizer')

def get_hf_maxlength(model: 'torchbenchmark.util.model.BenchmarkModel') -> Optional[int]:
    return model.max_length if is_hf_model(model) else None

def check_precision(model: 'torchbenchmark.util.model.BenchmarkModel', precision: str) -> bool:
    if precision == "fp16":
        return model.device == 'cuda' and hasattr(model, "enable_fp16_half")
    if precision == "tf32":
        return model.device == "cuda"
    if precision == "amp":
        if model.test == 'eval' and model.device == 'cuda':
            return True
        if model.test == 'train' and model.device == 'cuda':
            return hasattr(model, 'enable_amp') or is_staged_train_test(model)
    assert precision == "fp32", f"Expected precision to be one of fp32, tf32, fp16, or amp, but get {precision}"
    return True

def check_memory_layout(model: 'torchbenchmark.util.model.BenchmakModel', channels_last: bool) -> bool:
    if channels_last:
        return hasattr(model, 'enable_channels_last')
    return True

def check_distributed_trainer(model: 'torchbenchmark.util.model.BenchmakModel', distributed_trainer: Optional[str]) -> bool:
    if not model.test == "train" and distributed_trainer:
        return False
    return True

def get_precision_default(model: 'torchbenchmark.util.model.BenchmarkModel') -> str:
    if hasattr(model, "DEFAULT_EVAL_CUDA_PRECISION") and model.test == 'eval' and model.device == 'cuda':
        return model.DEFAULT_EVAL_CUDA_PRECISION
    if hasattr(model, "DEFAULT_TRAIN_CUDA_PRECISION") and model.test == 'train' and model.device == 'cuda':
        return model.DEFAULT_TRAIN_CUDA_PRECISION
    return "fp32"

def parse_decoration_args(model: 'torchbenchmark.util.model.BenchmarkModel', extra_args: List[str]) -> Tuple[argparse.Namespace, List[str]]:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--distributed",
        choices=["ddp", "ddp_no_static_graph", "fsdp"],
        default=None,
        help="Enable distributed trainer",
    )
    parser.add_argument(
        "--distributed_wrap_fn",
        type=str,
        default=None,
        help="Path to function that will apply distributed wrapping fn(model, dargs.distributed)",
    )
    parser.add_argument("--precision", choices=["fp32", "tf32", "fp16", "amp"], default=get_precision_default(model), help="choose precisions from: fp32, tf32, fp16, or amp")
    parser.add_argument("--channels-last", action='store_true', help="enable channels-last memory layout")
    parser.add_argument("--skip_correctness", action='store_true', help="Skip correctness checks")
    dargs, opt_args = parser.parse_known_args(extra_args)
    if not check_precision(model, dargs.precision):
        raise NotImplementedError(f"precision value: {dargs.precision}, "
                                  "fp16 is only supported if the model implements the `enable_fp16_half()` callback function."
                                  "amp is only supported if cuda+eval, or if `enable_amp` implemented,"
                                  "or if model uses staged train interfaces (forward, backward, optimizer).")
    if not check_memory_layout(model, dargs.channels_last):
        raise NotImplementedError(f"Specified channels_last: {dargs.channels_last} ,"
                                  f" but the model doesn't implement the enable_channels_last() interface.")
    if not check_distributed_trainer(model, dargs.distributed):
        raise NotImplementedError(f"We only support distributed trainer {dargs.distributed} for train tests, "
                                  f"but get test: {model.test}")
    return (dargs, opt_args)

def apply_decoration_args(model: 'torchbenchmark.util.model.BenchmarkModel', dargs: argparse.Namespace):
    if dargs.channels_last:
        model.enable_channels_last()
    if dargs.precision == "fp16":
        model.enable_fp16_half()
    elif dargs.precision == "tf32":
        import torch
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
    elif dargs.precision == "amp":
        # model handles amp itself if it has 'enable_amp' callback function (e.g. pytorch_unet)
        if hasattr(model, "enable_amp"):
            model.enable_amp()
        elif model.test == "eval":
            import torch
            model.add_context(lambda: torch.cuda.amp.autocast(dtype=torch.float16))
        elif model.test == "train":
            # the model must implement staged train test
            assert is_staged_train_test(model), f"Expected model implements staged train test (forward, backward, optimizer)."
            import torch
            model.add_context(lambda: torch.cuda.amp.autocast(dtype=torch.float16), stage=TEST_STAGE.FORWARD)
    elif not dargs.precision == "fp32":
        assert False, f"Get an invalid precision option: {dargs.precision}. Please report a bug."

# Dispatch arguments based on model type
def parse_opt_args(model: 'torchbenchmark.util.model.BenchmarkModel', opt_args: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=list_backends(), help="enable backends")
    parser.add_argument("--fx2trt", action='store_true', help="enable fx2trt")
    parser.add_argument("--fuser", type=str, default="", choices=["fuser0", "fuser1", "fuser2"], help="enable fuser")
    parser.add_argument("--torch_trt", action='store_true', help="enable torch_tensorrt")
    parser.add_argument("--flops", choices=["fvcore", "dcgm"], help="Return the flops result")
    parser.add_argument("--use_cosine_similarity", action='store_true', help="use cosine similarity for correctness check")
    parser.add_argument("--blade", action='store_true', help="enable blade optimize")
    args, extra_args = parser.parse_known_args(opt_args)
    if model.jit:
        args.backend = "torchscript"
    if model.device == "cpu" and args.fuser:
        raise NotImplementedError("Fuser only works with GPU.")
    if not (model.device == "cuda" and model.test == "eval"):
        if args.fx2trt or args.torch_trt:
            raise NotImplementedError("TensorRT only works for CUDA inference tests.")
    if is_torchvision_model(model):
        args.cudagraph = False
    return args, extra_args

def apply_opt_args(model: 'torchbenchmark.util.model.BenchmarkModel', args: argparse.Namespace, extra_args: List[str]):
    if args.flops == "fvcore":
        enable_fvcore_flops(model)
    if args.backend:
        backend = BACKENDS[args.backend]
        # transform the model using the specified backend
        backend(model, backend_args=extra_args)
        return
    assert not extra_args, f"Exptected no unknown args at this point, found {extra_args}"
    if args.fuser:
        import torch
        model.add_context(lambda: torch.jit.fuser(args.fuser))
    if args.fx2trt:
        if model.jit:
            raise NotImplementedError("fx2trt with JIT is not available.")
        module, exmaple_inputs = model.get_module()
        fp16 = not (model.dargs.precision == "fp32")
        model.set_module(enable_fx2trt(model.batch_size, fp16=fp16, model=module, example_inputs=exmaple_inputs,
                                       is_hf_model=is_hf_model(model), hf_max_length=get_hf_maxlength(model)))
    if args.torch_trt:
        module, exmaple_inputs = model.get_module()
        precision = 'fp16' if not model.dargs.precision == "fp32" else 'fp32'
        model.set_module(enable_torchtrt(precision=precision, model=module, example_inputs=exmaple_inputs))

