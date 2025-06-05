from celery.signals import worker_process_init

from App import App


@worker_process_init.connect
def init_worker_process(sender=None, **kwargs):
    """
    Initialize the App instance once when the worker process starts.
    This ensures we only create one instance per worker process.
    """
    # Get the App instance - this will create it if it doesn't exist
    app = App.get_instance()
