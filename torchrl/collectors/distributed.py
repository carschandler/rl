import os
import time
import submitit
import socket

from torch.distributed import rpc

from torchrl.collectors import MultiaSyncDataCollector
from torchrl.collectors.collectors import RandomPolicy
from torchrl.envs import EnvCreator
from torchrl.envs.vec_env import _BatchedEnv


def collect(rank, rank0_ip):
    os.environ["MASTER_ADDR"] = str(rank0_ip)
    os.environ["MASTER_PORT"] = "29500"
    os.environ["TORCH_DISTRIBUTED_DEBUG"] = "DETAIL"
    options = rpc.TensorPipeRpcBackendOptions(
        num_worker_threads=16,
        init_method=f"tcp://{rank0_ip}:10002",
        rpc_timeout=120,
        _transports=["uv"],
    )
    print("init rpc")
    rpc.init_rpc(
        f"COLLECTOR_NODE_{rank}",
        rank=rank,
        backend=rpc.BackendType.TENSORPIPE,
        rpc_backend_options=options,
    )
    print("waiting...")
    time.sleep(100)
    rpc.shutdown()


class DistributedDataCollector:
    def __init__(self, env_makers, policy, frames_per_batch, total_frames):
        self.env_constructors = env_makers
        self.num_workers = len(env_makers)
        self.frames_per_batch = frames_per_batch
        self.total_frames = total_frames

        hostname = socket.gethostname()
        IPAddr = socket.gethostbyname(hostname)
        print("IP address", IPAddr)
        os.environ["MASTER_ADDR"] = str(IPAddr)
        os.environ["MASTER_PORT"] = "29500"
        os.environ["TORCH_DISTRIBUTED_DEBUG"] = "DETAIL"
        options = rpc.TensorPipeRpcBackendOptions(
            num_worker_threads=16,
            init_method="tcp://localhost:10002",
            rpc_timeout=120,
            _transports=["uv"],
        )
        print("init rpc")
        rpc.init_rpc(
            "TRAINER_NODE",
            rank=0,
            backend=rpc.BackendType.TENSORPIPE,
            rpc_backend_options=options,
        )

    def _init_workers(self):
        self.collector_infos = []
        self.collector_rrefs = []
        for i in range(self.num_workers):
            print("Submitting job")
            executor = submitit.AutoExecutor(folder="log_test")
            executor.update_parameters(
                timeout_min=10,
                slurm_partition="train",
                slurm_cpus_per_task=32
                )
            job = executor.submit(collect, i+1, self.IPAddr)  # will compute add(5, 7)
            print("job id", job.job_id)  # ID of your job

            print("creating the collector")
            while True:
                time.sleep(1.0)
                try:
                    print("trying to connect to collector node")
                    collector_info = rpc.get_worker_info(f"COLLECTOR_NODE_{i+1}")
                    break
                except RuntimeError as err:
                    print(err)
                    continue
            env_make = self.env_constructors[i]
            if not isinstance(env_make, (EnvBase, EnvCreator)):
                env_make = EnvCreator(env_make)
            collector_rref = rpc.remote(
                collector_info,
                MultiaSyncDataCollector,
                args=([env_make] * self.num_envs_per_collector,
                      self.policy),
                kwargs={
                    "frames_per_batch": self.frames_per_batch,
                        "total_frames": self.total_frames, 
                        "split_trajs": False
                        },
            )
            self.collector_infos.append(collector_info)
            self.collector_rrefs.append(collector_rref)

    def iterator(self):
        total_frames = 0
        while total_frames < self.total_frames:
            future = rpc.rpc_async(
                self.collector_infos[i],
                MultiaSyncDataCollector.next,
                args=(collector_rref,)
            )
            total_frames += data.numel()