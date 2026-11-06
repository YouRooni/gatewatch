class DemoActions:
    capabilities = ['ban', 'unban']

    def __init__(self, config):
        pass

    async def execute(self, job):
        return {'ok':True,'detail':'Demo: firewall unchanged'}
