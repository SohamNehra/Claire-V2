"""Stop the Splash workers a live evaluation started.

Live evaluations run on throwaway state. A Splash worker is recorded as owned
in the state directory of the process that started it, so once the evaluation
exits the installed service can neither use nor unload that worker: it finds
the port taken by a server it has no record of and refuses to start its own.
llama-server and LM Studio models are shared by any Claire process and stay.
"""
import atexit

from claire_core import boot


def release_splash_on_exit(settings):
    atexit.register(_release, settings)


def _release(settings):
    result = boot.unload_models(settings, kinds=("splash",))
    for error in result["errors"]:
        print(f"  Splash worker left running: {error}", flush=True)
