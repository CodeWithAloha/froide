import json

from django.db import transaction
from django.contrib import admin
from django.shortcuts import redirect, render
from django.http import HttpResponse
from django.core.exceptions import PermissionDenied
from django.conf import settings
from django.db.models import Count
from django.utils.translation import gettext_lazy as _, ngettext
from django.utils import timezone
from django.urls import reverse_lazy
from django import forms
from django.urls import reverse
from django.urls import path
from django.utils.html import format_html
from django.template.response import TemplateResponse

from treebeard.admin import TreeAdmin
from treebeard.forms import movenodeform_factory
from parler.admin import TranslatableAdmin
from froide.helper.admin_utils import (
    make_batch_tag_action,
    make_emptyfilter,
    make_nullfilter,
    make_choose_object_action,
    TreeRelatedFieldListFilter,
)
from froide.helper.widgets import TagAutocompleteWidget
from froide.helper.search.utils import trigger_search_index_update_qs
from froide.helper.csv_utils import export_csv_response, dict_to_csv_stream

from .models import (
    PublicBody,
    PublicBodyTag,
    TaggedPublicBody,
    FoiLaw,
    Jurisdiction,
    Classification,
    Category,
    CategorizedPublicBody,
    ProposedPublicBody,
)
from .csv_import import CSVImporter


CATEGORY_AUTOCOMPLETE_URL = reverse_lazy("api:category-autocomplete")


class PublicBodyAdminForm(forms.ModelForm):
    class Meta:
        model = PublicBody
        fields = "__all__"
        widgets = {
            "categories": TagAutocompleteWidget(
                autocomplete_url=CATEGORY_AUTOCOMPLETE_URL
            ),
        }


def execute_replace_publicbody(admin, request, queryset, action_obj):
    """
    Replaces all non-blocklisted FK or M2M relationships
    that point to obj with assign_obj.
    Dark magic ahead.
    """
    BLOCK_LIST = [CategorizedPublicBody, TaggedPublicBody, PublicBody]
    relations = [
        f
        for f in PublicBody._meta.get_fields()
        if (f.one_to_many or f.one_to_one or f.many_to_many)
        and f.auto_created
        and not f.concrete
    ]
    for obj in queryset:
        with transaction.atomic():
            for rel in relations:
                model = rel.related_model
                if model in BLOCK_LIST:
                    continue
                if rel.many_to_many:
                    m2m_objs = model.objects.filter(**{rel.field.name: obj})
                    for m2m_obj in m2m_objs:
                        m2m_rel = getattr(m2m_obj, rel.field.name)
                        m2m_rel.remove(obj)
                        m2m_rel.add(action_obj)
                else:
                    model.objects.filter(**{rel.field.name: obj}).update(
                        **{rel.field.name: action_obj}
                    )


def execute_assign_classification(admin, request, queryset, action_obj):
    queryset.update(classification=action_obj)
    trigger_search_index_update_qs(queryset)


class PublicBodyBaseAdminMixin:
    form = PublicBodyAdminForm

    date_hierarchy = "updated_at"
    prepopulated_fields = {"slug": ("name",)}
    save_on_top = True
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "name",
                    "slug",
                    "other_names",
                    "classification",
                    "url",
                    "email",
                    "alternative_emails",
                    "fax",
                    "contact",
                    "address",
                )
            },
        ),
        (
            _("Context"),
            {
                "fields": (
                    "jurisdiction",
                    "laws",
                    "request_note",
                    "categories",
                    "description",
                    "file_index",
                    "org_chart",
                ),
            },
        ),
        (
            _("Hierachy"),
            {
                "classes": ("collapse",),
                "fields": ("parent", "root", "depth"),
            },
        ),
        (
            _("Geo"),
            {
                "classes": ("collapse",),
                "fields": ("regions", "geo"),
            },
        ),
        (
            _("Advanced"),
            {
                "classes": ("collapse",),
                "fields": (
                    "site",
                    "number_of_requests",
                    "website_dump",
                    "wikidata_item",
                    "source_reference",
                    "extra_data",
                    "change_proposals",
                ),
            },
        ),
        (
            _("Meta"),
            {
                "fields": (
                    "_created_by",
                    "created_at",
                    "_updated_by",
                    "updated_at",
                ),
            },
        ),
    )
    list_display = (
        "name",
        "email",
        "url",
        "classification",
        "jurisdiction",
        "category_list",
        "request_count",
    )
    list_filter = (
        make_emptyfilter(
            "change_proposals", _("Has change proposals"), empty_value=dict
        ),
        "jurisdiction",
        ("classification", TreeRelatedFieldListFilter),
        "classification",
        "categories",
        make_nullfilter("geo", _("Has geo coordinates")),
        make_nullfilter("regions", _("Has regions")),
        make_emptyfilter("email", "E-Mail"),
        make_emptyfilter("fax", "Fax"),
    )
    filter_horizontal = ("laws",)
    list_max_show_all = 5000
    search_fields = ["name", "other_names", "description", "email", "source_reference"]
    exclude = ("confirmed",)
    raw_id_fields = (
        "parent",
        "root",
        "_created_by",
        "_updated_by",
        "regions",
        "classification",
    )
    readonly_fields = ("_created_by", "created_at", "_updated_by", "updated_at")

    actions = (
        "assign_classification",
        "replace_publicbody",
        "export_csv",
        "remove_from_index",
        "tag_all",
        "show_georegions",
        "validate_publicbodies",
    )

    tag_all = make_batch_tag_action(
        field="categories", autocomplete_url=CATEGORY_AUTOCOMPLETE_URL
    )

    assign_classification = make_choose_object_action(
        Classification, execute_assign_classification, _("Assign classification...")
    )
    replace_publicbody = make_choose_object_action(
        PublicBody, execute_replace_publicbody, _("Replace public bodies with...")
    )

    def get_queryset(self, request):
        qs = super(PublicBodyBaseAdminMixin, self).get_queryset(request)
        qs = qs.annotate(request_count=Count("foirequest"))
        qs = qs.select_related("classification", "jurisdiction")
        return qs

    def request_count(self, obj):
        return obj.request_count

    request_count.admin_order_field = "request_count"
    request_count.short_description = _("requests")

    def get_urls(self):
        urls = super(PublicBodyBaseAdminMixin, self).get_urls()
        my_urls = [
            path(
                "import/",
                self.admin_site.admin_view(self.import_csv),
                name="publicbody-publicbody-import_csv",
            ),
            path(
                "geo-match/",
                self.admin_site.admin_view(self.geo_match),
                name="publicbody-publicbody-geo_match",
            ),
        ]
        return my_urls + urls

    def import_csv(self, request):
        if not request.method == "POST":
            raise PermissionDenied
        if not self.has_change_permission(request):
            raise PermissionDenied

        importer = CSVImporter()
        url = request.POST.get("url")
        csv_file = request.FILES.get("file")
        try:
            if not url and not csv_file:
                raise ValueError(_("You need to provide a url or a file."))
            if url:
                importer.import_from_url(url)
            else:
                importer.import_from_file(csv_file)
        except Exception as e:
            self.message_user(request, str(e))
        else:
            self.message_user(request, _("Public Bodies were imported."))
        return redirect("admin:publicbody_publicbody_changelist")

    def geo_match(self, request):
        from froide.georegion.models import GeoRegion

        if request.method == "POST":
            if not self.has_change_permission(request):
                raise PermissionDenied

            data = json.loads(request.body)
            try:
                georegion = GeoRegion.objects.get(id=data["georegion"])
            except GeoRegion.DoesNotExist:
                return HttpResponse(status=404)
            try:
                pb = PublicBody.objects.get(id=data["publicbody"])
            except PublicBody.DoesNotExist:
                return HttpResponse(status=404)

            pb.regions.add(georegion)
            return HttpResponse(status=201, content=b"{}")

        opts = self.model._meta
        config = {
            "url": {
                "listCategories": reverse("api:category-list"),
                "listClassifications": reverse("api:classification-list"),
                "listPublicBodies": reverse("api:publicbody-list"),
                "searchPublicBody": reverse("api:publicbody-search"),
                "listGeoregion": reverse("api:georegion-list"),
                "detailGeoregion": reverse("api:georegion-detail", kwargs={"pk": 0}),
                "detailJurisdiction": reverse(
                    "api:jurisdiction-detail", kwargs={"pk": 0}
                ),
                "georegionAdminUrl": reverse(
                    "admin:georegion_georegion_change", kwargs={"object_id": 0}
                ),
                "publicbodyAdminUrl": reverse("admin:publicbody_publicbody_changelist"),
                "publicbodyAdminChangeUrl": reverse(
                    "admin:publicbody_publicbody_change", kwargs={"object_id": 0}
                ),
                "publicbodyAddAdminUrl": reverse("admin:publicbody_publicbody_add"),
            }
        }
        ctx = {"app_label": opts.app_label, "opts": opts, "config": json.dumps(config)}
        return render(request, "publicbody/admin/match_georegions.html", ctx)

    def save_model(self, request, obj, form, change):
        obj._updated_by = request.user
        obj.updated_at = timezone.now()
        if change is None:
            obj._created_by = obj._updated_by
            obj.created_at = obj.updated_at

        super(PublicBodyBaseAdminMixin, self).save_model(request, obj, form, change)

    def category_list(self, obj):
        return ", ".join(o.name for o in obj.categories.all())

    def export_csv(self, request, queryset):
        return export_csv_response(PublicBody.export_csv(queryset))

    export_csv.short_description = _("Export to CSV")

    def remove_from_index(self, request, queryset):
        from django_elasticsearch_dsl.registries import registry

        for obj in queryset:
            registry.delete(obj, raise_on_error=False)

        self.message_user(request, _("Removed from search index"))

    remove_from_index.short_description = _("Remove from search index")

    def show_georegions(self, request, queryset):
        opts = self.model._meta

        context = {
            "opts": opts,
            "media": self.media,
            "applabel": opts.app_label,
            "no_regions": queryset.filter(regions=None),
            "regions": json.dumps(
                {
                    reg.id: pb.id
                    for pb in queryset.exclude(regions=None)
                    for reg in pb.regions.all()
                }
            ),
        }

        # Display the confirmation page
        return TemplateResponse(
            request, "publicbody/admin/show_georegions.html", context
        )

    show_georegions.short_description = _("Show georegions of")

    def validate_publicbodies(self, request, queryset):
        from .validators import validate_publicbodies

        csv_stream = dict_to_csv_stream(validate_publicbodies(queryset))
        return export_csv_response(csv_stream, name="validation.csv")


class PublicBodyAdminMixin(PublicBodyBaseAdminMixin):
    def get_queryset(self, request):
        qs = super(PublicBodyAdminMixin, self).get_queryset(request)
        qs = qs.filter(confirmed=True)
        return qs


class PublicBodyAdmin(PublicBodyAdminMixin, admin.ModelAdmin):
    pass


class ProposedPublicBodyAdminMixin(PublicBodyBaseAdminMixin):
    list_display = (
        "name",
        "email",
        "url",
        "classification",
        "jurisdiction",
        "created_by",
        "created_at",
    )
    date_hierarchy = "created_at"
    actions = ["confirm_selected"]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.select_related("_created_by")
        return qs

    def get_urls(self):
        urls = super().get_urls()
        my_urls = [
            path(
                "<int:pk>/confirm/",
                self.admin_site.admin_view(self.confirm),
                name="publicbody-proposedpublicbody-confirm",
            ),
            path(
                "<int:pk>/send-message/",
                self.admin_site.admin_view(self.send_message),
                name="publicbody-proposedpublicbody-send_message",
            ),
        ]
        return my_urls + urls

    def confirm(self, request, pk):
        if not request.method == "POST":
            raise PermissionDenied
        if not self.has_change_permission(request):
            raise PermissionDenied

        pb = ProposedPublicBody.objects.get(pk=pk)

        result = self._confirm_pb(pb, request.user)

        if result is None:
            self.message_user(request, _("This public body is already confirmed."))
        else:
            self.message_user(
                request,
                ngettext(
                    "Public body confirmed. %(count)d message was sent.",
                    "Public body confirmed. %(count)d messages were sent",
                    result,
                )
                % {"count": result},
            )

        return redirect("admin:publicbody_publicbody_change", pb.id)

    def _confirm_pb(self, pb, user):
        pb._updated_by = user
        pb.updated_at = timezone.now()
        result = pb.confirm(user=user)

        creator = pb.created_by
        if result is not None and creator and creator != user:
            creator.send_mail(
                _("Public body “%s” has been approved") % pb.name,
                _(
                    "Hello,\n\nYou can find the approved public body here:\n\n"
                    "{url}\n\nAll the Best,\n{site_name}"
                ).format(
                    url=pb.get_absolute_domain_url(), site_name=settings.SITE_NAME
                ),
                priority=False,
            )
        return result

    def confirm_selected(self, request, queryset):
        queryset = queryset.filter(confirmed=False)
        for pb in queryset:
            self._confirm_pb(pb, request.user)

        self.message_user(
            request, _("{} public bodies were confirmed.").format(queryset.count())
        )

    confirm_selected.short_description = _("Confirm all selected")

    def send_message(self, request, pk):
        if not request.method == "POST":
            raise PermissionDenied
        if not self.has_change_permission(request):
            raise PermissionDenied

        pb = ProposedPublicBody.objects.get(pk=pk)

        creator = pb.created_by
        if creator:
            creator.send_mail(
                _("Concerning your public body proposal “%s”") % pb.name,
                request.POST.get("message"),
                priority=False,
            )
            self.message_user(request, _("E-Mail was sent to public body creator."))
        return redirect("admin:publicbody_proposedpublicbody_change", pb.id)


class ProposedPublicBodyAdmin(ProposedPublicBodyAdminMixin, admin.ModelAdmin):
    pass


class FoiLawAdmin(TranslatableAdmin):
    list_display = (
        "name",
        "meta",
        "priority",
        "law_type",
        "jurisdiction",
    )
    list_filter = ("meta", "law_type", "jurisdiction")
    raw_id_fields = ("mediator", "combined")
    filter_horizontal = ("combined",)
    search_fields = ["translations__name", "translations__description"]

    def get_prepopulated_fields(self, request, obj=None):
        # can't use `prepopulated_fields = ..` because it breaks the admin validation
        # for translated fields. This is the official django-parler workaround.
        return {"slug": ("name",)}

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.select_related("jurisdiction")
        qs = qs.prefetch_related(
            "translations",
            "combined",
            "combined__translations",
            "combined__jurisdiction",
        )
        return qs


class JurisdictionAdmin(admin.ModelAdmin):
    prepopulated_fields = {"slug": ("name",)}
    list_filter = [
        "hidden",
        "rank",
        make_nullfilter("region", _("Has region")),
    ]
    list_display = ["name", "hidden", "rank"]
    raw_id_fields = ("region",)


class PublicBodyTagAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "is_topic", "rank"]
    list_filter = ["is_topic", "rank"]
    ordering = ["rank", "name"]
    search_fields = ["name"]
    prepopulated_fields = {"slug": ["name"]}


class TaggedPublicBodyAdmin(admin.ModelAdmin):
    raw_id_fields = ("content_object", "tag")


def execute_assign_parent(admin, request, queryset, action_obj):
    for obj in queryset:
        obj.move(action_obj, "sorted-child")


assign_classification_parent = make_choose_object_action(
    Classification, execute_assign_parent, _("Assign parent...")
)

assign_category_parent = make_choose_object_action(
    Category, execute_assign_parent, _("Assign parent...")
)


class ClassificationAdmin(TreeAdmin):
    fields = (
        "name",
        "slug",
        "_position",
        "_ref_node_id",
    )
    form = movenodeform_factory(Classification)
    prepopulated_fields = {"slug": ["name"]}
    search_fields = ["name"]
    list_display = ("name", "num_publicbodies", "publicbody_link")
    actions = ["assign_parent"]

    assign_parent = assign_classification_parent

    def get_queryset(self, request):
        """Use this so we can annotate with additional info."""

        qs = super(ClassificationAdmin, self).get_queryset(request)
        return qs.annotate(num_publicbodies=Count("publicbody", distinct=True))

    def num_publicbodies(self, obj):
        """# of companies an expert has."""

        return obj.num_publicbodies

    num_publicbodies.short_description = _("# public bodies")

    def publicbody_link(self, obj):
        return format_html(
            '<a href="{}">{}</a>',
            reverse("admin:publicbody_publicbody_changelist")
            + ("?classification__id__exact={}".format(obj.id)),
            _("Public bodies with this classification"),
        )


class CategoryAdmin(TreeAdmin):
    fields = (
        "name",
        "slug",
        "is_topic",
        "_position",
        "_ref_node_id",
    )

    form = movenodeform_factory(Category)
    prepopulated_fields = {"slug": ["name"]}
    search_fields = ["name"]
    list_filter = ("is_topic", "depth")
    list_display = ("name", "is_topic", "num_publicbodies", "publicbody_link")
    actions = ["assign_parent"]

    assign_parent = assign_category_parent

    def get_queryset(self, request):
        """Use this so we can annotate with additional info."""

        qs = super(CategoryAdmin, self).get_queryset(request)
        return qs.annotate(
            num_publicbodies=Count("categorized_publicbodies", distinct=True)
        )

    def num_publicbodies(self, obj):
        """# of companies an expert has."""

        return obj.num_publicbodies

    num_publicbodies.short_description = _("# public bodies")

    def publicbody_link(self, obj):
        return format_html(
            '<a href="{}">{}</a>',
            reverse("admin:publicbody_publicbody_changelist")
            + ("?categories__id__exact={}".format(obj.id)),
            _("Public bodies with this category"),
        )


class CategorizedPublicBodyAdmin(admin.ModelAdmin):
    raw_id_fields = ("content_object", "tag")


admin.site.register(PublicBody, PublicBodyAdmin)
admin.site.register(ProposedPublicBody, ProposedPublicBodyAdmin)
admin.site.register(FoiLaw, FoiLawAdmin)
admin.site.register(Jurisdiction, JurisdictionAdmin)
admin.site.register(PublicBodyTag, PublicBodyTagAdmin)
admin.site.register(TaggedPublicBody, TaggedPublicBodyAdmin)
admin.site.register(Classification, ClassificationAdmin)
admin.site.register(Category, CategoryAdmin)
admin.site.register(CategorizedPublicBody, CategorizedPublicBodyAdmin)
