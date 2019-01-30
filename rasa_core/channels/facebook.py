import hashlib
import hmac
import logging
from typing import Text, List, Dict, Any, Callable

from fbmessenger import (
    BaseMessenger, MessengerClient, attachments)
from fbmessenger.elements import Text as FBText
from flask import Blueprint, request, jsonify

from rasa_core.channels.channel import UserMessage, OutputChannel, InputChannel

logger = logging.getLogger(__name__)


class Messenger(BaseMessenger):
    """Implement a fbmessenger to parse incoming webhooks and send msgs."""

    @classmethod
    def name(cls):
        return "facebook"

    def __init__(self,
                 page_access_token: Text,
                 on_new_message: Callable[[UserMessage], None],
                 thread_control_authorized: List[int]) -> None:

        self.page_access_token = page_access_token
        self.on_new_message = on_new_message
        self.thread_control_authorized = thread_control_authorized
        super(Messenger, self).__init__(self.page_access_token)

    @staticmethod
    def _is_audio_message(message: Dict[Text, Any]) -> bool:
        """Check if the users message is a recorced voice message."""
        return (message.get('message') and
                message['message'].get('attachments') and
                message['message']['attachments'][0]['type'] == 'audio')

    @staticmethod
    def _is_user_message(message: Dict[Text, Any]) -> bool:
        """Check if the message is a message from the user"""
        return (message.get('message') and
                message['message'].get('text') and
                not message['message'].get("is_echo"))

    @staticmethod
    def _is_user_location(message: Dict[Text, Any]) -> bool:
        """Check if the users message is a recorced voice message."""
        return (message.get('message') and
                message['message'].get('attachments') and
                message['message']['attachments'][0]['type'] == 'location')

    @staticmethod
    def _is_request_thread_control(message: Dict[Text, Any]) -> bool:
        """Check if facebook is requesting thread control."""
        return (message.get('request_thread_control') and
                message['request_thread_control'].get('requested_owner_app_id'))

    @staticmethod
    def _is_pass_thread_control(message: Dict[Text, Any]) -> bool:
        """Check if facebook is passing thread control."""
        return (message.get('pass_thread_control') and
                message['pass_thread_control'].get('new_owner_app_id'))

    def message(self, message: Dict[Text, Any]) -> None:
        """Handle an incoming event from the fb webhook."""
        print("Message: {}".format(message))

        if self._is_user_message(message):
            text = message['message']['text']

            if len(message['message']['text']) > 512:
                self._handle_user_message('/long_text', self.get_user_id())
                return

        elif self._is_audio_message(message):
            attachment = message['message']['attachments'][0]
            text = attachment['payload']['url']
        elif self._is_user_location(message):
            attachment = message['message']['attachments'][0]
            coordinates = attachment['payload']['coordinates']
            text = "{} {}".format(coordinates['lat'], coordinates['long'])
        else:
            logger.warning("Received a message from facebook that we can not "
                           "handle. Message: {}".format(message))
            return

        self._handle_user_message(text, self.get_user_id())

    def postback(self, message: Dict[Text, Any]) -> None:
        """Handle a postback (e.g. quick reply button)."""
        print("Handling postback: {}".format(message))

        text = message['postback']['payload']
        self._handle_user_message(text, self.get_user_id())

    def _handle_user_message(self, text: Text, sender_id: Text) -> None:
        """Pass on the text to the dialogue engine for processing."""
        print("Handling User Message: {}".format(text))

        out_channel = MessengerBot(self.client)
        user_msg = UserMessage(text, out_channel, sender_id,
                               input_channel=self.name())

        # noinspection PyBroadException
        try:
            self.on_new_message(user_msg)
        except Exception:
            logger.exception("Exception when trying to handle webhook "
                             "for facebook message.")
            pass

    def delivery(self, message: Dict[Text, Any]) -> None:
        """Do nothing. Method to handle `message_deliveries`"""
        pass

    def read(self, message: Dict[Text, Any]) -> None:
        """Do nothing. Method to handle `message_reads`"""
        pass

    def account_linking(self, message: Dict[Text, Any]) -> None:
        """Do nothing. Method to handle `account_linking`"""
        pass

    def optin(self, message: Dict[Text, Any]) -> None:
        """Do nothing. Method to handle `messaging_optins`"""
        pass

    def handover(self, message: Dict[Text, Any]) -> None:

        print("Handover: {}".format(message))

        if self._is_request_thread_control(message):
            if not message['request_thread_control']['requested_owner_app_id'] in self.thread_control_authorized:
                logger.warning("Received a request thread control from facebook"
                               "from an app that is not authorized: {}".format(message))
                pass
            else:
                MessengerBot(self.client).send_pass_thread_control(self.get_user_id(), message['request_thread_control']['requested_owner_app_id'], message['request_thread_control']['metadata'])
                self._handle_user_message('/passed_thread_control', self.get_user_id())

        elif self._is_pass_thread_control(message):
            self._handle_user_message('/reconnect_user', self.get_user_id())


class MessengerBot(OutputChannel):
    """A bot that uses fb-messenger to communicate."""

    @classmethod
    def name(cls):
        return "facebook"

    def __init__(self, messenger_client: MessengerClient) -> None:

        self.messenger_client = messenger_client
        super(MessengerBot, self).__init__()

    def send(self, recipient_id: Text, element: Any) -> None:
        """Sends a message to the recipient using the messenger client."""

        # this is a bit hacky, but the client doesn't have a proper API to
        # send messages but instead expects the incoming sender to be present
        # which we don't have as it is stored in the input channel.
        self.messenger_client.send(element.to_dict(),
                                   {"sender": {"id": recipient_id}},
                                   'RESPONSE')

    def send_text_message(self, recipient_id: Text, message: Text) -> None:
        """Send a message through this channel."""

        logger.info("Sending message: " + message)

        for message_part in message.split("\n\n"):
            self.send(recipient_id, FBText(text=message_part))

    def send_image_url(self, recipient_id: Text, image_url: Text) -> None:
        """Sends an image. Default will just post the url as a string."""

        self.send(recipient_id, attachments.Image(url=image_url))

    def send_text_with_buttons(self, recipient_id: Text, text: Text,
                               buttons: List[Dict[Text, Any]],
                               **kwargs: Any) -> None:
        """Sends buttons to the output."""

        # buttons is a list of tuples: [(option_name,payload)]
        if len(buttons) > 3:
            logger.warning(
                "Facebook API currently allows only up to 3 buttons. "
                "If you add more, all will be ignored.")
            self.send_text_message(recipient_id, text)
        else:
            self._add_postback_info(buttons)

            # Currently there is no predefined way to create a message with
            # buttons in the fbmessenger framework - so we need to create the
            # payload on our own
            payload = {
                "attachment": {
                    "type": "template",
                    "payload": {
                        "template_type": "button",
                        "text": text,
                        "buttons": buttons
                    }
                }
            }
            self.messenger_client.send(payload,
                                       {"sender": {"id": recipient_id}},
                                       'RESPONSE')

    def send_quick_replies(self, recipient_id, text, quick_replies, **kwargs):
        # type: (Text, Text, List[Dict[Text, Any]], Any) -> None
        """Sends quick replies to the output."""

        self._add_text_info(quick_replies)

        # Currently there is no predefined way to create a message with
        # custom quick_replies in the fbmessenger framework - so we need to create the
        # payload on our own
        payload = {
            "text": text,
            "quick_replies": quick_replies
        }
        self.messenger_client.send(payload,
                                   {"sender": {"id": recipient_id}},
                                   'RESPONSE')

    def send_pass_thread_control(self, recipient_id, app_id, metadata, **kwargs):
        print("Recipient ID: {}".format(recipient_id))
        self.messenger_client.pass_thread_control(app_id,
                                                  metadata,
                                                  {"recipient": {"id": recipient_id}})

    def send_take_thread_control(self, recipient_id, metadata, **kwargs):
        print("Take Control - Recipient ID: {}".format(recipient_id))
        self.messenger_client.take_thread_control(metadata,
                                                  {"recipient": {"id": recipient_id}})

    def send_custom_message(self, recipient_id: Text,
                            elements: List[Dict[Text, Any]]) -> None:
        """Sends elements to the output."""

        for element in elements:
            self._add_postback_info(element['buttons'])

        payload = {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "elements": elements
                }
            }
        }
        self.messenger_client.send(payload,
                                   self._recipient_json(recipient_id),
                                   'RESPONSE')

    @staticmethod
    def _add_postback_info(buttons: List[Dict[Text, Any]]) -> None:
        """Make sure every button has a type. Modifications happen in place."""
        for button in buttons:
            if 'type' not in button:
                button['type'] = "postback"

    @staticmethod
    def _add_text_info(quick_replies):
        # type: (List[Dict[Text, Any]]) -> None
        """Set the quick reply type to text for all buttons without content type.
        Happens in place."""
        for quick_reply in quick_replies:
            if 'content_type' not in quick_reply:
                quick_reply['content_type'] = "text"

    @staticmethod
    def _recipient_json(recipient_id: Text) -> Dict[Text, Dict[Text, Text]]:
        """Generate the response json for the recipient expected by FB."""
        return {"sender": {"id": recipient_id}}


class FacebookInput(InputChannel):
    """Facebook input channel implementation. Based on the HTTPInputChannel."""

    @classmethod
    def name(cls):
        return "facebook"

    @classmethod
    def from_credentials(cls, credentials):
        if not credentials:
            cls.raise_missing_credentials_exception()

        return cls(credentials.get("verify"),
                   credentials.get("secret"),
                   credentials.get("page-access-token"),
                   credentials.get("thread-control-authorized"))

    def __init__(self, fb_verify: Text, fb_secret: Text,
                 fb_access_token: Text, fb_thread_control_authorized: List[int]) -> None:
        """Create a facebook input channel.

        Needs a couple of settings to properly authenticate and validate
        messages. Details to setup:

        https://github.com/rehabstudio/fbmessenger#facebook-app-setup

        Args:
            fb_verify: FB Verification string
                (can be chosen by yourself on webhook creation)
            fb_secret: facebook application secret
            fb_access_token: access token to post in the name of the FB page
        """
        self.fb_verify = fb_verify
        self.fb_secret = fb_secret
        self.fb_access_token = fb_access_token
        self.fb_thread_control_authorized = fb_thread_control_authorized

    def blueprint(self, on_new_message):

        fb_webhook = Blueprint('fb_webhook', __name__)

        @fb_webhook.route("/", methods=['GET'])
        def health():
            return jsonify({"status": "ok"})

        @fb_webhook.route("/webhook", methods=['GET'])
        def token_verification():
            if request.args.get("hub.verify_token") == self.fb_verify:
                return request.args.get("hub.challenge")
            else:
                logger.warning(
                    "Invalid fb verify token! Make sure this matches "
                    "your webhook settings on the facebook app.")
                return "failure, invalid token"

        @fb_webhook.route("/webhook", methods=['POST'])
        def webhook():
            signature = request.headers.get("X-Hub-Signature") or ''
            if not self.validate_hub_signature(self.fb_secret, request.data,
                                               signature):
                logger.warning("Wrong fb secret! Make sure this matches the "
                               "secret in your facebook app settings")
                return "not validated"

            messenger = Messenger(self.fb_access_token, on_new_message, self.fb_thread_control_authorized)

            messenger.handle(request.get_json(force=True))
            return "success"

        return fb_webhook

    @staticmethod
    def validate_hub_signature(app_secret, request_payload,
                               hub_signature_header):
        """Make sure the incoming webhook requests are properly signed.

        Args:
            app_secret: Secret Key for application
            request_payload: request body
            hub_signature_header: X-Hub-Signature header sent with request

        Returns:
            bool: indicated that hub signature is validated
        """

        # noinspection PyBroadException
        try:
            hash_method, hub_signature = hub_signature_header.split('=')
        except Exception:
            pass
        else:
            digest_module = getattr(hashlib, hash_method)
            hmac_object = hmac.new(
                bytearray(app_secret, 'utf8'),
                request_payload, digest_module)
            generated_hash = hmac_object.hexdigest()
            if hub_signature == generated_hash:
                return True
        return False
