# maubot_gmailhooks
Maubot plugin for subscribing to and receiving room specific webhooks from gmail pub/sub

It will require that you have created an apps script project (and corresponding pub/sub cloud project) as described here to use this; https://github.com/palchrb/gmail_webhook/tree/main

You can configure the backend in the maubot config with; a webapp https address to the apps script dopost, and a token for provisioning.
By then sending !gmail commands to the bot it will allow you to start subscriptions to all emails sendt to a certain gmail + address.

So the way it works;
- !gmail sub testhook
- It will then talk to the provisioning endpoint in the google apps script backend, send a webhook and bearer token to it - and create a plus-address with alice+testhook-randomstring@gmail.com
- You can then send any email to the +-address, e.g. use it to subscribe to email alerts from providers that don't offer webhooks directly -  and gmail will recognize it and forward a webhook the your room.
- Jinja templates can be used to format the webhook/email content
