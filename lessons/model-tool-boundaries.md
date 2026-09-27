Model supplied tool arguments should return recoverable validation errors when a correction is possible.

The live profiler requested more than 120 lines, and an exception from `read_repo_file`
aborted the entire agent graph before it could retry. Returning a bounded error message
keeps the file read limit while allowing the model to correct its call.
The same live suite later waited more than ten minutes for one model HTTPS read.
Setting a request timeout at agent creation bounds provider stalls so failed runs
can reach their scoped cleanup and leave a repeatable artifact.
