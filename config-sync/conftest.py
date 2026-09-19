from hypothesis import settings, Verbosity
import os

settings.register_profile(
    "dev",
    max_examples=15,
    verbosity=Verbosity.normal,
    deadline=None,
)
settings.register_profile(
    "ci",
    max_examples=100,
    verbosity=Verbosity.verbose,
    deadline=None,
)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "dev"))
