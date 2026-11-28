"""Short-lived public read views; authoritative operations never use this cache."""
import threading,time
from collections import OrderedDict
from apps.api.infrastructure.store import clone_json
from apps.api.modules.scoring import Scores

MAX_AGE_SECONDS=2.0
MAX_VIEWS=128
_INITIALIZE=threading.Lock()

class PublicViews:
    def __init__(self,store):
        self.store=store
        self.lock=threading.Lock()
        self.revision=None
        self.expires=0.0
        self.scores=None
        self.views=OrderedDict()

    def read(self,key,build):
        # Separate from Store's process/file locks: cache hits never block a
        # submission or verification, nor can callers mutate cached results.
        with self.lock:
            revision=self.store.public_revision()
            valid=not revision[1] and revision==self.revision and time.monotonic()<self.expires
            if valid and key in self.views:
                self.views.move_to_end(key)
                return clone_json(self.views[key]),True
            with self.store.transaction() as tx:
                revision=self.store.public_revision()
                valid=not revision[1] and revision==self.revision and time.monotonic()<self.expires
                scores=self.scores if valid else Scores(tx)
                value=build(tx,scores)
            # The transaction's token was captured before releasing its lock.
            # Some legacy readers persist phase/milestone metadata. Never
            # retain a pre-write scoring context under their post-write token.
            if tx.writes:
                self.views.clear();self.scores=None;self.revision=None;self.expires=0
                return clone_json(value),False
            if not valid:self.views.clear()
            self.scores=scores
            self.revision=tx.committed_revision
            if not valid:self.expires=time.monotonic()+MAX_AGE_SECONDS
            self.views[key]=clone_json(value)
            self.views.move_to_end(key)
            while len(self.views)>MAX_VIEWS:self.views.popitem(last=False)
            return clone_json(value),False


def read(store,key,build,response):
    started=time.perf_counter()
    with _INITIALIZE:
        cache=getattr(store,'_public_views',None)
        if cache is None:
            cache=PublicViews(store);store._public_views=cache
    result,hit=cache.read(key,build)
    response.headers['X-Public-Cache']='hit' if hit else 'miss'
    response.headers['Server-Timing']=f'public_view;dur={(time.perf_counter()-started)*1000:.2f}'
    response.headers['Cache-Control']='no-store'
    return result
