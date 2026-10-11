# Persisted Requests are serialized with msgspec, and Callbacks are referenced by method name

Requests entering a persistent Frontier are serialized with msgspec and carry a schema version; `meta` must be JSON-serializable. A Callback is referenced by the name of a Spider method. If the method cannot be found on resume, it is an error; there is no fallback to the default Callback.

## Why

- **No pickle**:
  - It turns "can write to the queue" into "can execute code on a Worker".
  - Renaming or moving a class makes old data unloadable.
  - Newly added attributes are missing on old objects.
- **No silent fallback**: falling back would hand data to the wrong parsing logic without any error.
