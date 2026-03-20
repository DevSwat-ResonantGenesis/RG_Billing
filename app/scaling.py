"""
Horizontal Scaling Support - Phase 4.4 GTM

Support for horizontal scaling of billing service.
"""

import logging
import os
import socket
import hashlib
from datetime import datetime
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Try to import Redis for distributed coordination
try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


@dataclass
class InstanceInfo:
    """Information about a service instance."""
    instance_id: str
    hostname: str
    port: int
    started_at: str
    last_heartbeat: str
    status: str
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "hostname": self.hostname,
            "port": self.port,
            "started_at": self.started_at,
            "last_heartbeat": self.last_heartbeat,
            "status": self.status,
        }


class DistributedLock:
    """
    Distributed lock using Redis.
    
    Ensures only one instance processes a resource at a time.
    """
    
    def __init__(self, redis_client, lock_timeout: int = 30):
        self.redis = redis_client
        self.lock_timeout = lock_timeout
        self._locks: Dict[str, str] = {}  # In-memory fallback
    
    async def acquire(self, resource: str, owner: str) -> bool:
        """
        Acquire a lock on a resource.
        
        Args:
            resource: Resource identifier
            owner: Lock owner identifier
            
        Returns:
            True if lock acquired
        """
        key = f"lock:{resource}"
        
        if self.redis and REDIS_AVAILABLE:
            try:
                # SET NX with expiry
                result = await self.redis.set(
                    key, owner,
                    nx=True,
                    ex=self.lock_timeout
                )
                return result is not None
            except Exception as e:
                logger.error(f"Redis lock error: {e}")
        
        # In-memory fallback (not distributed!)
        if key not in self._locks:
            self._locks[key] = owner
            return True
        return self._locks[key] == owner
    
    async def release(self, resource: str, owner: str) -> bool:
        """
        Release a lock on a resource.
        
        Args:
            resource: Resource identifier
            owner: Lock owner identifier
            
        Returns:
            True if lock released
        """
        key = f"lock:{resource}"
        
        if self.redis and REDIS_AVAILABLE:
            try:
                # Only release if we own the lock
                current = await self.redis.get(key)
                if current and current.decode() == owner:
                    await self.redis.delete(key)
                    return True
                return False
            except Exception as e:
                logger.error(f"Redis unlock error: {e}")
        
        # In-memory fallback
        if key in self._locks and self._locks[key] == owner:
            del self._locks[key]
            return True
        return False
    
    async def extend(self, resource: str, owner: str, seconds: int = 30) -> bool:
        """
        Extend a lock's timeout.
        
        Args:
            resource: Resource identifier
            owner: Lock owner identifier
            seconds: Seconds to extend
            
        Returns:
            True if extended
        """
        key = f"lock:{resource}"
        
        if self.redis and REDIS_AVAILABLE:
            try:
                current = await self.redis.get(key)
                if current and current.decode() == owner:
                    await self.redis.expire(key, seconds)
                    return True
                return False
            except Exception as e:
                logger.error(f"Redis extend error: {e}")
        
        return key in self._locks and self._locks[key] == owner


class ServiceRegistry:
    """
    Service registry for instance discovery.
    
    Allows instances to register themselves and discover peers.
    """
    
    def __init__(self, redis_client=None, ttl: int = 60):
        self.redis = redis_client
        self.ttl = ttl
        self._instances: Dict[str, InstanceInfo] = {}
        self._instance_id = self._generate_instance_id()
    
    def _generate_instance_id(self) -> str:
        """Generate unique instance ID."""
        hostname = socket.gethostname()
        pid = os.getpid()
        return hashlib.md5(f"{hostname}:{pid}:{datetime.utcnow().isoformat()}".encode()).hexdigest()[:12]
    
    @property
    def instance_id(self) -> str:
        return self._instance_id
    
    async def register(self, port: int = 8000) -> InstanceInfo:
        """
        Register this instance.
        
        Args:
            port: Service port
            
        Returns:
            InstanceInfo for this instance
        """
        hostname = socket.gethostname()
        now = datetime.utcnow().isoformat()
        
        info = InstanceInfo(
            instance_id=self._instance_id,
            hostname=hostname,
            port=port,
            started_at=now,
            last_heartbeat=now,
            status="active",
        )
        
        if self.redis and REDIS_AVAILABLE:
            try:
                key = f"service:billing:{self._instance_id}"
                await self.redis.setex(
                    key,
                    self.ttl,
                    str(info.to_dict())
                )
            except Exception as e:
                logger.error(f"Redis register error: {e}")
        
        self._instances[self._instance_id] = info
        logger.info(f"Registered instance: {self._instance_id}")
        
        return info
    
    async def heartbeat(self) -> bool:
        """
        Send heartbeat to keep registration alive.
        
        Returns:
            True if heartbeat successful
        """
        if self._instance_id not in self._instances:
            return False
        
        info = self._instances[self._instance_id]
        info.last_heartbeat = datetime.utcnow().isoformat()
        
        if self.redis and REDIS_AVAILABLE:
            try:
                key = f"service:billing:{self._instance_id}"
                await self.redis.setex(
                    key,
                    self.ttl,
                    str(info.to_dict())
                )
                return True
            except Exception as e:
                logger.error(f"Redis heartbeat error: {e}")
        
        return True
    
    async def deregister(self):
        """Deregister this instance."""
        if self.redis and REDIS_AVAILABLE:
            try:
                key = f"service:billing:{self._instance_id}"
                await self.redis.delete(key)
            except Exception as e:
                logger.error(f"Redis deregister error: {e}")
        
        if self._instance_id in self._instances:
            del self._instances[self._instance_id]
        
        logger.info(f"Deregistered instance: {self._instance_id}")
    
    async def get_instances(self) -> List[InstanceInfo]:
        """
        Get all registered instances.
        
        Returns:
            List of InstanceInfo
        """
        instances = []
        
        if self.redis and REDIS_AVAILABLE:
            try:
                keys = await self.redis.keys("service:billing:*")
                for key in keys:
                    data = await self.redis.get(key)
                    if data:
                        # Parse instance info
                        info_dict = eval(data.decode())
                        instances.append(InstanceInfo(**info_dict))
            except Exception as e:
                logger.error(f"Redis get instances error: {e}")
        
        # Include local instances
        for info in self._instances.values():
            if not any(i.instance_id == info.instance_id for i in instances):
                instances.append(info)
        
        return instances


class ConsistentHash:
    """
    Consistent hashing for distributing work across instances.
    
    Ensures same user always goes to same instance for cache efficiency.
    """
    
    def __init__(self, replicas: int = 100):
        self.replicas = replicas
        self._ring: Dict[int, str] = {}
        self._sorted_keys: List[int] = []
    
    def _hash(self, key: str) -> int:
        """Generate hash for a key."""
        return int(hashlib.md5(key.encode()).hexdigest(), 16)
    
    def add_node(self, node: str):
        """Add a node to the ring."""
        for i in range(self.replicas):
            key = self._hash(f"{node}:{i}")
            self._ring[key] = node
        self._sorted_keys = sorted(self._ring.keys())
    
    def remove_node(self, node: str):
        """Remove a node from the ring."""
        for i in range(self.replicas):
            key = self._hash(f"{node}:{i}")
            if key in self._ring:
                del self._ring[key]
        self._sorted_keys = sorted(self._ring.keys())
    
    def get_node(self, key: str) -> Optional[str]:
        """
        Get the node responsible for a key.
        
        Args:
            key: Key to look up
            
        Returns:
            Node identifier or None
        """
        if not self._ring:
            return None
        
        hash_key = self._hash(key)
        
        # Find first node with hash >= key hash
        for ring_key in self._sorted_keys:
            if ring_key >= hash_key:
                return self._ring[ring_key]
        
        # Wrap around to first node
        return self._ring[self._sorted_keys[0]]


class ScalingManager:
    """
    Manager for horizontal scaling features.
    
    Coordinates distributed locking, service discovery,
    and work distribution.
    """
    
    def __init__(self, redis_url: Optional[str] = None):
        self.redis_client = None
        self.redis_url = redis_url
        
        if redis_url and REDIS_AVAILABLE:
            try:
                self.redis_client = redis.from_url(redis_url)
            except Exception as e:
                logger.error(f"Failed to connect to Redis: {e}")
        
        self.lock = DistributedLock(self.redis_client)
        self.registry = ServiceRegistry(self.redis_client)
        self.hash_ring = ConsistentHash()
    
    async def initialize(self, port: int = 8000):
        """
        Initialize scaling manager.
        
        Args:
            port: Service port
        """
        # Register this instance
        info = await self.registry.register(port)
        
        # Add to hash ring
        self.hash_ring.add_node(info.instance_id)
        
        # Discover other instances
        instances = await self.registry.get_instances()
        for instance in instances:
            if instance.instance_id != info.instance_id:
                self.hash_ring.add_node(instance.instance_id)
        
        logger.info(f"Scaling manager initialized: {len(instances)} instances")
    
    async def shutdown(self):
        """Shutdown scaling manager."""
        await self.registry.deregister()
        self.hash_ring.remove_node(self.registry.instance_id)
        
        if self.redis_client:
            await self.redis_client.close()
    
    def should_process(self, user_id: str) -> bool:
        """
        Check if this instance should process a user's request.
        
        Uses consistent hashing to ensure same user always
        goes to same instance.
        
        Args:
            user_id: User ID
            
        Returns:
            True if this instance should process
        """
        target = self.hash_ring.get_node(user_id)
        return target == self.registry.instance_id or target is None
    
    async def acquire_user_lock(self, user_id: str) -> bool:
        """
        Acquire lock for processing a user's request.
        
        Args:
            user_id: User ID
            
        Returns:
            True if lock acquired
        """
        return await self.lock.acquire(
            f"user:{user_id}",
            self.registry.instance_id
        )
    
    async def release_user_lock(self, user_id: str) -> bool:
        """
        Release lock for a user.
        
        Args:
            user_id: User ID
            
        Returns:
            True if released
        """
        return await self.lock.release(
            f"user:{user_id}",
            self.registry.instance_id
        )
    
    async def get_cluster_status(self) -> Dict[str, Any]:
        """
        Get cluster status.
        
        Returns:
            Cluster status information
        """
        instances = await self.registry.get_instances()
        
        return {
            "instance_id": self.registry.instance_id,
            "total_instances": len(instances),
            "instances": [i.to_dict() for i in instances],
            "redis_connected": self.redis_client is not None,
            "hash_ring_nodes": len(self.hash_ring._ring) // self.hash_ring.replicas,
        }


# Global instance (initialized on startup)
scaling_manager: Optional[ScalingManager] = None


def get_scaling_manager() -> Optional[ScalingManager]:
    """Get the global scaling manager."""
    return scaling_manager


async def init_scaling(redis_url: Optional[str] = None, port: int = 8000):
    """
    Initialize scaling support.
    
    Args:
        redis_url: Redis URL for coordination
        port: Service port
    """
    global scaling_manager
    scaling_manager = ScalingManager(redis_url)
    await scaling_manager.initialize(port)


async def shutdown_scaling():
    """Shutdown scaling support."""
    global scaling_manager
    if scaling_manager:
        await scaling_manager.shutdown()
        scaling_manager = None
