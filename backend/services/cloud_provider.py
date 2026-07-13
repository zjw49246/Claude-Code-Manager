"""云实例管理抽象层（elastic-worker 设计 §16.2）。

AWSProvider 通过 IMDSv2 + boto3 从 Manager 自身实例元数据自举配置
（机型/AMI/子网/密钥对继承本机，"配置和本机一样"），凭证优先走 IAM
instance profile。所有 boto3 调用是同步的，统一用 asyncio.to_thread 包装。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx

logger = logging.getLogger(__name__)

IMDS_BASE = "http://169.254.169.254/latest"


class CloudProvider(ABC):
    @abstractmethod
    async def self_describe(self) -> dict:
        """返回 Manager 自身实例信息（用于配置自举）。"""

    @abstractmethod
    async def create_instance(self, name: str, overrides: dict | None = None) -> str:
        """创建实例，返回 instance_id。"""

    @abstractmethod
    async def describe_instance(self, instance_id: str) -> dict:
        """返回 {state, private_ip, public_ip, instance_type, name}。"""

    @abstractmethod
    async def wait_until_running(self, instance_id: str, timeout: int = 300) -> str:
        """等实例 Running，返回 private_ip。"""

    @abstractmethod
    async def stop_instance(self, instance_id: str) -> None: ...

    @abstractmethod
    async def start_instance(self, instance_id: str) -> None: ...

    @abstractmethod
    async def terminate_instance(self, instance_id: str) -> None: ...


class AWSProvider(CloudProvider):
    def __init__(self, region: str | None = None):
        self._region = region
        self._client: Any = None
        self._self_info: dict | None = None

    # -- 内部工具 ---------------------------------------------------------

    async def _imds(self, path: str) -> str:
        async with httpx.AsyncClient(timeout=5) as c:
            token = (await c.put(
                f"{IMDS_BASE}/api/token",
                headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
            )).text
            r = await c.get(
                f"{IMDS_BASE}/meta-data/{path}",
                headers={"X-aws-ec2-metadata-token": token},
            )
            r.raise_for_status()
            return r.text

    async def _ec2(self) -> Any:
        if self._client is None:
            if self._region is None:
                self._region = await self._imds("placement/region")
            import boto3  # 延迟导入：未装 boto3 时其余功能不受影响

            self._client = await asyncio.to_thread(
                boto3.client, "ec2", region_name=self._region
            )
        return self._client

    @staticmethod
    def _parse(inst: dict) -> dict:
        name = next(
            (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), None
        )
        return {
            "instance_id": inst["InstanceId"],
            "state": inst["State"]["Name"],
            "private_ip": inst.get("PrivateIpAddress"),
            "public_ip": inst.get("PublicIpAddress"),
            "instance_type": inst.get("InstanceType"),
            "image_id": inst.get("ImageId"),
            "subnet_id": inst.get("SubnetId"),
            "key_name": inst.get("KeyName"),
            "security_group_ids": [g["GroupId"] for g in inst.get("SecurityGroups", [])],
            "name": name,
        }

    # -- 接口实现 ---------------------------------------------------------

    async def self_describe(self) -> dict:
        if self._self_info is None:
            my_id = await self._imds("instance-id")
            self._self_info = await self.describe_instance(my_id)
        return self._self_info

    async def describe_instance(self, instance_id: str) -> dict:
        ec2 = await self._ec2()
        # AWS 最终一致性：run_instances 返回 ID 后 describe 可能短暂 NotFound
        for attempt in range(8):
            try:
                resp = await asyncio.to_thread(
                    ec2.describe_instances, InstanceIds=[instance_id]
                )
                if resp["Reservations"]:
                    return self._parse(resp["Reservations"][0]["Instances"][0])
            except Exception as e:
                if "InvalidInstanceID" not in str(e) or attempt >= 7:
                    raise
            await asyncio.sleep(3)
        raise RuntimeError(f"instance {instance_id} not found after 8 attempts")

    async def create_instance(self, name: str, overrides: dict | None = None) -> str:
        me = await self.self_describe()
        overrides = overrides or {}
        params = {
            "ImageId": overrides.get("image_id") or me["image_id"],
            "InstanceType": overrides.get("instance_type") or me["instance_type"],
            "SubnetId": overrides.get("subnet_id") or me["subnet_id"],
            "KeyName": overrides.get("key_name") or me["key_name"],
            "SecurityGroupIds": overrides.get("security_group_ids")
            or me["security_group_ids"],
            "MinCount": 1,
            "MaxCount": 1,
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": "Name", "Value": name}],
                }
            ],
        }
        ec2 = await self._ec2()
        resp = await asyncio.to_thread(ec2.run_instances, **params)
        iid = resp["Instances"][0]["InstanceId"]
        logger.info("created worker instance %s (%s)", iid, name)
        return iid

    async def wait_until_running(self, instance_id: str, timeout: int = 300) -> str:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            info = await self.describe_instance(instance_id)
            if info["state"] == "running" and info["private_ip"]:
                return info["private_ip"]
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(
                    f"instance {instance_id} not running after {timeout}s "
                    f"(state={info['state']})"
                )
            await asyncio.sleep(5)

    async def stop_instance(self, instance_id: str) -> None:
        ec2 = await self._ec2()
        await asyncio.to_thread(ec2.stop_instances, InstanceIds=[instance_id])

    async def start_instance(self, instance_id: str) -> None:
        ec2 = await self._ec2()
        await asyncio.to_thread(ec2.start_instances, InstanceIds=[instance_id])

    async def terminate_instance(self, instance_id: str) -> None:
        ec2 = await self._ec2()
        await asyncio.to_thread(ec2.terminate_instances, InstanceIds=[instance_id])

    async def update_instance_tags(self, instance_id: str, tags: dict) -> None:
        """Update tags on an EC2 instance via ec2.create_tags()."""
        ec2 = await self._ec2()
        tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
        await asyncio.to_thread(
            ec2.create_tags, Resources=[instance_id], Tags=tag_list
        )


def get_cloud_provider(provider_name: str) -> CloudProvider:
    providers: dict[str, type[CloudProvider]] = {"aws": AWSProvider}
    if provider_name not in providers:
        raise ValueError(f"unsupported cloud provider: {provider_name}")
    return providers[provider_name]()
