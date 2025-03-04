"""
Support TorchDynamo(https://github.com/facebookresearch/torchdynamo) backends
"""
import argparse
import contextlib
import distutils.util
from typing import List
import torch
try:
    import torch._dynamo as torchdynamo
except ImportError:
    import torchdynamo
from torchbenchmark.util.model import is_staged_train_test
import warnings
import functools
from typing import List
from .blade import blade_optimize_dynamo

TORCHDYNAMO_ROUNDS = 3
def parse_torchdynamo_args(model: 'torchbenchmark.util.model.BenchmarkModel', dynamo_args: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    available_backends = torchdynamo.list_backends()
    parser.add_argument(
        "--torchdynamo", choices=available_backends, help="Specify torchdynamo backends"
    )
    parser.add_argument(
        "--tritonmm", type=str, help="torchinductor.config.triton.mm configuration"
    )
    parser.add_argument(
        "--optimize_dynamo_ddp",
        action='store_true',
        help="enable extra optimizations for DDP + dynamo"
    )
    parser.add_argument(
        "--torchinductor_cudagraph",
        type=distutils.util.strtobool,
        default="true",
    )
    parser.add_argument(
        "--torchinductor_fallback_random",
        type=distutils.util.strtobool,
        default="false",
    )
    parser.add_argument(
        "--dynamo_disable_optimizer_step",
        type=distutils.util.strtobool,
        default="false",
    )
    parser.add_argument(
        "--trt",
        action='store_true',
        help="use blade tensorrt backend",
    )
    args, extra_args = parser.parse_known_args(dynamo_args)
    return args, extra_args

def apply_torchdynamo_args(model: 'torchbenchmark.util.model.BenchmarkModel', args: argparse.Namespace, precision: str):
    # torchdynamo.config.suppress_errors = True
    if hasattr(torchdynamo.config, 'DO_NOT_USE_legacy_non_fake_example_inputs'):
        torchdynamo.config.DO_NOT_USE_legacy_non_fake_example_inputs = True
    torchdynamo.reset()
    torchdynamo.utils.counters.clear()

    if args.torchdynamo == "fx2trt" and precision == "fp16":
        dynamo_optimizer = torchdynamo.optimize(torchdynamo.optimizations.backends.fx2trt_compiler_fp16)
    elif "blade" in args.torchdynamo:
        dynamo_optimizer = torchdynamo.optimize(functools.partial(blade_optimize_dynamo, enable_fp16=precision=="fp16", use_trt=args.trt))
    elif "ipex" in args.torchdynamo and precision == "fp32":
        dynamo_optimizer = torchdynamo.optimize(torchdynamo.optimizations.backends.ipex_fp32)
    else:
        dynamo_optimizer = torchdynamo.optimize(args.torchdynamo)

    if args.torchdynamo == "inductor":
        try:
            import torch._inductor as torchinductor
        except ImportError:
            import torchinductor
            import torchinductor.config
        torchinductor.config.triton.cudagraphs = bool(args.torchinductor_cudagraph)

        # Setup torchinductor.config.triton.mm
        if args.tritonmm == "triton":
            torchinductor.config.triton.mm = "triton"
            # currently can't pass correctness with use_bmm = True
            # torchinductor.config.triton.use_bmm = True

        # used for correctness checks, to avoid triton rand() behaving differently from torch rand().
        torchinductor.config.fallback_random = bool(args.torchinductor_fallback_random)

    if bool(args.dynamo_disable_optimizer_step):
        found_optimizer_step = False
        try:
            model.cfg.optimizer.step = torchdynamo.disable(model.cfg.optimizer.step)
            found_optimizer_step = True
        except AttributeError:
            pass

        try:
            model.optimizer.step = torchdynamo.disable(model.optimizer.step)
            found_optimizer_step = True
        except AttributeError:
            pass

        if not found_optimizer_step:
            warnings.warn("--dynamo_disable_optimizer_step is set to True, but the optimizer could not be found on this model")

    if model.test == "train":
        if is_staged_train_test(model):
            model.forward = dynamo_optimizer(model.forward)
        else:
            model.train = dynamo_optimizer(model.train)
    else:
        model.eval = dynamo_optimizer(model.eval)

    if args.optimize_dynamo_ddp:
        @contextlib.contextmanager
        def optimize_ddp_ctx(val: bool):
            old_value = torchdynamo.config.optimize_ddp
            try:
                torchdynamo.config.optimize_ddp = val
                yield
            finally:
                torchdynamo.config.optimize_ddp = old_value
        model.add_context(lambda: optimize_ddp_ctx(True))

    torchdynamo.reset()
    
    for _ in range(TORCHDYNAMO_ROUNDS):
        model.invoke()
