Profile Git symlinks from link metadata instead of following them.

An external-repository E2E fixture exposed that reading a tracked symlink followed
it outside the repository. Profiling the link target string and hashing the link
itself keeps the source repository read-only and the profile within its scope.
