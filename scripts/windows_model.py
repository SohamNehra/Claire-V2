"""Select the sole downloaded LM Studio model for a fresh Windows install.

Never changes an existing choice. Multiple downloaded models stay a user choice.
"""
from claire_core import models
from claire_core.settings import Settings


def configure():
    settings = Settings()
    saved = models.load(settings)
    if saved['active'] or saved['models']:
        return
    library = models.lmstudio_library(settings)
    if library is None:
        raise RuntimeError('LM Studio is not answering on port 1234.')
    if len(library) != 1:
        print('Choose your downloaded model on the Models page before chatting.')
        return
    model = library[0]
    entry = models.add(settings, 'lmstudio', model=model['key'], label=model['name'],
                       context=min(model.get('context') or 32768, 32768))
    models.choose(settings, entry['name'])
    print('Selected LM Studio model: ' + entry['label'])


if __name__ == '__main__':
    configure()
