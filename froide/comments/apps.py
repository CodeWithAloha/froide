import json

from django.apps import AppConfig
from django.utils.translation import ugettext_lazy as _


class CommentConfig(AppConfig):
    name = 'froide.comments'
    verbose_name = _('Comments')

    def ready(self):
        from froide.account import account_canceled
        from froide.account.export import registry

        account_canceled.connect(cancel_user)
        registry.register(export_user_data)


def cancel_user(sender, user=None, **kwargs):
    from .models import FroideComment

    if user is None:
        return
    FroideComment.objects.filter(user=user).update(
        user_name='',
        user_email='',
        user_url=''
    )


def export_user_data(user):
    from .models import FroideComment

    comments = FroideComment.objects.filter(user=user)
    if not comments:
        return
    yield ('comments.json', json.dumps([
        {
            'submit_date': (
                c.submit_date.isoformat() if c.submit_date else None
            ),
            'comment': c.comment,
            'is_public': c.is_public,
            'is_removed': c.is_removed,
            'url': c.get_absolute_url(),
        }
        for c in comments]).encode('utf-8')
    )
