"""Bounded background work for one review session; no Streamlit calls here."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from time import monotonic


LAYERS = {
    "true_color": "True color",
    "cloud": "Cloud / shadow mask",
    "ndvi": "NDVI",
    "lst": "LST (°C)",
    "eci": "ECI",
}
DEFAULT_VISIBLE = ("true_color", "cloud")


class SceneLoader:
    """Keep running requests, replace obsolete queued work, and cache results.

    Only the most recent plan is submitted as workers become free. Running
    Earth Engine requests cannot be cancelled, but their results are keyed by
    scene and can never overwrite another scene. Injected fetchers must be plain
    functions: workers never read session state or call Streamlit caches/UI.
    """

    def __init__(self, tile_fetcher, stats_fetcher, *, ttl=1800,
                 max_entries=80, max_workers=4, clock=monotonic):
        self._tile_fetcher = tile_fetcher
        self._stats_fetcher = stats_fetcher
        self._ttl = ttl
        self._max_entries = max_entries
        self._max_workers = max_workers
        self._clock = clock
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="scene-loader"
        )
        self._lock = RLock()
        self._cache = OrderedDict()
        self._running = {}
        self._wanted = []
        self._closed = False

    def activate(self, anchor, visible=DEFAULT_VISIBLE,
                 preferred="true_color", neighbors=()):
        """Prioritize the selected layer, then other visible layers and extras."""
        selected = [key for key in visible if key in LAYERS]
        if preferred in selected:
            selected.remove(preferred)
            selected.insert(0, preferred)
        current = [(anchor, key) for key in selected]
        extras = [(anchor, key) for key in LAYERS if key not in selected]
        with self._lock:
            self._expire()
            # Neighbors only start after this scene's visible layers are ready.
            ready = all(self._cache.get(key, {}).get("value") is not None
                        for key in current)
            prefetch = [(tuple(scene), key) for scene in neighbors[:2]
                        for key in selected] if ready else []
            self._wanted = list(dict.fromkeys(
                current + extras + [(anchor, "stats")] + prefetch
            ))
            self._pump()

    def _expire(self):
        now = self._clock()
        for key, entry in list(self._cache.items()):
            if now - entry["completed_at"] >= self._ttl:
                del self._cache[key]

    def _pump(self):
        if self._closed:
            return
        for key in self._wanted:
            if len(self._running) >= self._max_workers:
                break
            if key in self._cache or key in self._running:
                continue
            anchor, layer = key
            fetcher = self._stats_fetcher if layer == "stats" else self._tile_fetcher
            args = anchor if layer == "stats" else (*anchor, layer)
            future = self._pool.submit(fetcher, *args)
            self._running[key] = future
            future.add_done_callback(lambda done, key=key: self._complete(key, done))

    def _complete(self, key, future):
        try:
            entry = {"value": future.result(), "error": None}
        except Exception as exc:  # remote failures belong to this layer only
            entry = {"value": None, "error": f"{type(exc).__name__}: {exc}"}
        with self._lock:
            self._running.pop(key, None)
            entry["completed_at"] = self._clock()
            self._cache[key] = entry
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_entries:
                # Retain current work in preference to old/preloaded scenes.
                victim = next((k for k in self._cache if k not in self._wanted),
                              next(iter(self._cache)))
                del self._cache[victim]
            self._pump()

    def snapshot(self, anchor):
        """Return completed results only. Never wait for an unfinished future."""
        with self._lock:
            self._expire()
            return {layer: dict(self._cache[(anchor, layer)])
                    for layer in (*LAYERS, "stats")
                    if (anchor, layer) in self._cache}

    def retry(self, anchor, layer):
        with self._lock:
            self._cache.pop((anchor, layer), None)
            self._pump()

    def close(self):
        """Stop queued work; running network calls finish without UI access."""
        with self._lock:
            self._closed = True
            self._wanted = []
        self._pool.shutdown(wait=False, cancel_futures=True)
