# maubot_gmailhooks
Maubot plugin for subscribing to and receiving room specific webhooks from gmail pub/sub. Currently it's mainly a provisioning bot that will allow you to pass a maubot webhook url and bearer token to an apps script backend. It does not generate the webhook and token as of now, but relies on copy paste from e.g. my own https://github.com/palchrb/maubot_roomhook or another webhook provider in matrix or maubot.

It will require that you have created an apps script project (and corresponding pub/sub cloud project) as described here to use this; https://github.com/palchrb/gmail_webhook/tree/main

You can configure the backend in the maubot config with; a webapp https address to the apps script dopost, and a token for provisioning.
By then sending !gmail commands to the bot it will allow you to start subscriptions to all emails sendt to a certain gmail + address.

So the way it works;
- ```!gmail sub <short name for your sub> <webhook url> <token>```
- It will then talk to the provisioning endpoint in the google apps script backend, send your webhook url, bearer token to it - and create a plus-address with alice+testhook-randomstring@gmail.com
- You can then send any email to the +-address, e.g. use it to subscribe to email alerts from providers that don't offer webhooks directly -  and gmail will recognize it and forward a webhook the your room.
- Hopefully, the webhook you have passed to it has jinja templating so you can tweak how the output looks!

I might later integrate the webhook creation etc into the plugin, so you can just initiate a sub with a name - and then the plugin creates webhook url and provisions with the gmail backend, but for now i thought it was easier and cleaner to keep this simple and rather rely on my existing roomwebhook for creating and receiving the webhooks themselves
