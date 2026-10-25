"""Bounded, thread-safe login throttling. Proxy headers are not treated as identity."""
import hashlib
import threading
import time
from .errors import ServiceError

class LoginLimiter:
    def __init__(self):
        self.buckets={}
        self.lock=threading.Lock()

    def check(self,host,username,ip_limit,account_limit):
        keys=[('ip',hashlib.sha256(host.encode()).hexdigest(),ip_limit),
              ('account',hashlib.sha256(username.strip().casefold().encode()).hexdigest(),account_limit)]
        now=time.monotonic()
        with self.lock:
            self.buckets={key:value for key,value in self.buckets.items() if now-value[0]<60}
            if len(self.buckets)+sum((kind,key) not in self.buckets for kind,key,_ in keys)>4096:
                raise ServiceError('LOGIN_CAPACITY_EXCEEDED','登录请求过多，请稍后重试。',429,True,60)
            for kind,key,limit in keys:
                started,count=self.buckets.get((kind,key),(now,0))
                if count>=limit:
                    raise ServiceError('LOGIN_RATE_LIMITED','登录尝试过于频繁，请稍后重试。',429,True,max(1,int(60-now+started)))
            for kind,key,_ in keys:
                started,count=self.buckets.get((kind,key),(now,0))
                self.buckets[(kind,key)]=(started,count+1)
