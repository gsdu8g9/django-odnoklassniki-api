# -*- coding: utf-8 -*-
from abc import abstractmethod
from datetime import datetime, date
import logging
import re

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models, transaction, IntegrityError
from django.db.models.fields import FieldDoesNotExist
from django.db.models.query import QuerySet
from django.utils.six import string_types
import fields
from pytz import timezone, utc

from .decorators import atomic
from .fields_api import API_REQUEST_FIELDS
from .utils import api_call, OdnoklassnikiError

log = logging.getLogger('odnoklassniki_api')

COMMIT_REMOTE = getattr(settings, 'ODNOKLASSNIKI_API_COMMIT_REMOTE', True)
MASTER_DATABASE = getattr(settings, 'ODNOKLASSNIKI_API_MASTER_DATABASE', 'default')


class OdnoklassnikiDeniedAccessError(Exception):
    pass


class OdnoklassnikiContentError(Exception):
    pass


class OdnoklassnikiParseError(Exception):
    pass


class OdnoklassnikiManager(models.Manager):

    '''
    Odnoklassniki Ads API Manager for RESTful CRUD operations
    '''
    fields = API_REQUEST_FIELDS

    def get_request_fields(self, *args, **kwargs):
        fields = []
        for arg in args:
            for field in self.fields.get(arg, ''):
                if kwargs.get('prefix', False):
                    field = '%s.%s' % (arg, field)
                fields += [field]
        return ','.join(fields)

    def __init__(self, methods=None, remote_pk=None, *args, **kwargs):
        if methods and len(methods.items()) < 1:
            raise ValueError('Argument methods must contains at least 1 specified method')

        self.methods = methods or {}
        self.remote_pk = remote_pk or ('id',)

        super(OdnoklassnikiManager, self).__init__(*args, **kwargs)

    def get_by_url(self, url):
        '''
        Return existed User, Group, Application by url or new intance with empty pk
        '''
        m = re.findall(r'^(?:https?://)?(?:www.)?(?:ok.ru|odnoklassniki.ru)/(.+)/?$', url)
        if not len(m):
            raise ValueError("Wrong domain: %s" % url)

        slug = m[0]

        try:
            assert self.model.slug_prefix and slug.startswith(self.model.slug_prefix)
            id = int(re.findall(r'^%s(\d+)$' % self.model.slug_prefix, slug)[0])
        except (AssertionError, ValueError, IndexError):
            try:
                response = api_call('url.getInfo', url=url)
                assert self.model.resolve_screen_name_type == response['type']
                id = int(response['objectId'])
            except OdnoklassnikiError, e:
                log.error("Method get_by_slug returned error instead of response. URL='%s'. Error: %s" % (url, e))
                return None
            except (KeyError, TypeError, ValueError), e:
                log.error("Method get_by_slug returned response in strange format: %s. URL='%s'" % (response, url))
                return None
            except AssertionError:
                log.error("Method get_by_slug returned instance with wrong type '%s', not '%s'. URL='%s'" %
                          (response['type'], self.model.resolve_screen_name_type, url))
                return None

        try:
            object = self.model.objects.get(id=id)
        except self.model.DoesNotExist:
            object = self.model(id=id)  # , shortname=slug)

        return object

    def get_or_create_from_instances_list(self, instances):
        # python 2.6 compatibility
        # return
        # self.model.objects.filter(pk__in={self.get_or_create_from_instance(instance).pk
        # for instance in instances})
        return self.model.objects.filter(pk__in=set([self.get_or_create_from_instance(instance).pk for instance in instances]))

    def get_or_create_from_resources_list(self, response_list, extra_fields=None):
        instances = self.parse_response_list(response_list, extra_fields)
        return self.get_or_create_from_instances_list(instances)

    def get_or_create_from_instance(self, instance):

        remote_pk_dict = {}
        for field_name in self.remote_pk:
            remote_pk_dict[field_name] = getattr(instance, field_name)

        if remote_pk_dict:
            try:
                old_instance = self.model.objects.using(MASTER_DATABASE).get(**remote_pk_dict)
                instance._substitute(old_instance)
                instance.save()
            except self.model.DoesNotExist:
                instance.save()
                log.debug('Fetch and create new object %s with remote pk %s' % (self.model, remote_pk_dict))
        else:
            instance.save()
            log.debug('Fetch and create new object %s without remote pk' % (self.model,))

        return instance

    def get_or_create_from_resource(self, resource):

        instance = self.model()
        instance.parse(dict(resource))

        return self.get_or_create_from_instance(instance)

    def api_call(self, method='get', **kwargs):
        if self.model.methods_access_tag:
            kwargs['methods_access_tag'] = self.model.methods_access_tag

        method = self.methods[method]
        if self.model.methods_namespace:
            method = self.model.methods_namespace + '.' + method

        return api_call(method, **kwargs)

    @atomic
    def fetch(self, *args, **kwargs):
        '''
        Retrieve and save object to local DB
        '''
        result = self.get(*args, **kwargs)
        if isinstance(result, list):
            return self.get_or_create_from_instances_list(result)
        elif isinstance(result, QuerySet):
            return result
        else:
            return self.get_or_create_from_instance(result)

    def get(self, *args, **kwargs):
        '''
        Retrieve objects from remote server
        TODO: rename everywhere extra_fields to _extra_fields
        '''
        extra_fields = kwargs.pop('extra_fields', {})
        extra_fields['fetched'] = datetime.utcnow().replace(tzinfo=utc)

        self.response = self.api_call(*args, **kwargs)

        return self.parse_response(self.response, extra_fields)

    def parse_response(self, response, extra_fields=None):
        if isinstance(response, (list, tuple)):
            return self.parse_response_list(response, extra_fields)
        elif isinstance(response, dict):
            return self.parse_response_dict(response, extra_fields)
        else:
            raise OdnoklassnikiContentError('Odnoklassniki response should be list or dict, not %s' % response)

    # TODO: rename to parse_response_object
    def parse_response_dict(self, resource, extra_fields=None):

        instance = self.model()
        # important to do it before calling parse method
        if extra_fields:
            instance.__dict__.update(extra_fields)
        instance.parse(resource)

        return instance

    def parse_response_list(self, response_list, extra_fields=None):

        instances = []
        for resource in response_list:

            # in response with stats there is extra array inside each element
            if isinstance(resource, list) and len(resource):
                resource = resource[0]

            # in some responses first value is `count` of all values:
            # http://vk.com/developers.php?oid=-1&p=groups.search
            if isinstance(resource, int):
                continue

            try:
                resource = dict(resource)
            except (TypeError, ValueError), e:
                log.error("Resource %s is not dictionary" % resource)
                raise e

            instance = self.parse_response_dict(resource, extra_fields)
            instances += [instance]

        return instances


class OdnoklassnikiTimelineManager(OdnoklassnikiManager):

    '''
    Manager class, child of OdnoklassnikiManager for fetching objects with arguments `after`, `before`
    '''
    timeline_cut_fieldname = 'date'
    timeline_force_ordering = False

    def get_timeline_date(self, instance):
        return getattr(instance, self.timeline_cut_fieldname, datetime(1970, 1, 1).replace(tzinfo=utc))

    @atomic
    def get(self, *args, **kwargs):
        '''
        Retrieve objects and return result list with respect to parameters:
         * 'after' - excluding all items before.
         * 'before' - excluding all items after.
        '''
        after = kwargs.pop('after', None)
        before = kwargs.pop('before', None)

        result = super(OdnoklassnikiTimelineManager, self).get(*args, **kwargs)

        if self.timeline_force_ordering:
            result.sort(key=self.get_timeline_date, reverse=True)

        instances = []
        for instance in result:

            timeline_date = self.get_timeline_date(instance)

            if timeline_date and isinstance(timeline_date, datetime):

                if after and after > timeline_date:
                    break

                if before and before < timeline_date:
                    continue

            instances += [instance]

        return instances


class OdnoklassnikiModel(models.Model):

    class Meta:
        abstract = True

    resolve_screen_name_type = ''
    remote_pk_field = 'id'
    remote_pk_local_field = 'id'
    methods_access_tag = ''
    methods_namespace = ''
    slug_prefix = ''

    fetched = models.DateTimeField(u'Обновлено', null=True, blank=True, db_index=True)

    objects = models.Manager()

    def _substitute(self, old_instance):
        '''
        Substitute new instance with old one while updating in method Manager.get_or_create_from_instance()
        Can be overrided in child models
        '''
        self.pk = old_instance.pk

        # substitute all valueble fields fom old_instance
        for key, old_value in old_instance.__dict__.items():
            new_value = getattr(self, key)
            if old_value and (new_value is None or new_value == ''):
                setattr(self, key, old_value)

    def parse(self, response):
        '''
        Parse API response and define fields with values
        '''
        for key, value in response.items():
            key = key.lower()
            if key == self.remote_pk_field:
                key = self.remote_pk_local_field
#                value = int(value)

            try:
                field = self._meta.get_field(key)
            except FieldDoesNotExist:
                log.debug('Field with name "%s" doesn\'t exists in the model %s' % (key, self.__class__.__name__))
                continue

            if isinstance(field, models.IntegerField) and value:
                try:
                    value = int(value)
                except:
                    pass
            elif isinstance(field, models.FloatField) and value:
                try:
                    value = float(value)
                except:
                    pass
            elif isinstance(field, models.CharField):
                if isinstance(value, bool):
                    value = ''
                else:
                    try:
                        value = unicode(value)
                    except:
                        pass

            elif isinstance(field, (models.DateTimeField, models.DateField)):

                if isinstance(value, string_types) and len(value) >= 10:
                    try:
                        if len(value) == 19:
                            value = datetime(int(value[0:4]), int(value[5:7]), int(value[8:10]), int(
                                value[11:13]), int(value[14:16]), int(value[17:19]))
                        elif len(value) == 16:
                            value = datetime(int(value[0:4]), int(value[5:7]), int(
                                value[8:10]), int(value[11:13]), int(value[14:16]))
                        elif len(value) == 10:
                            value = datetime(int(value[0:4]), int(value[5:7]), int(value[8:10]))
                        value = timezone('Europe/Moscow').localize(value)
                        assert value.year != 1970
                    except:
                        value = None
                else:
                    try:
                        value = int(value)
                        assert value > 0
                        value = datetime.utcfromtimestamp(value).replace(tzinfo=utc)
                    except:
                        value = None

            if isinstance(field, (models.OneToOneField, models.ForeignKey)) and value:
                rel_class = field.rel.to
                if isinstance(value, dict):
                    value = rel_class().parse(dict(value))
                else:
                    try:
                        value = rel_class.objects.get(pk=value)
                    except rel_class.DoesNotExist:
                        key = key + '_id'

            if isinstance(field, (fields.CommaSeparatedCharField, models.CommaSeparatedIntegerField)) and isinstance(value, list):
                value = ','.join([unicode(v) for v in value])

            setattr(self, key, value)

    def refresh(self):
        """
        Refresh current model with remote data
        """
        objects = self.__class__.remote.fetch_one(**self.refresh_kwargs)
        if isinstance(objects, models.Model):
            new_instance = objects
        elif isinstance(objects, (list, tuple, QuerySet)):
            if len(objects) == 1:
                new_instance = objects[0]
            else:
                raise OdnoklassnikiContentError(
                    "Remote server returned more objects, than expected - %d instead of one. Object details: %s, request details: %s" % (len(objects), self.__dict__, kwargs))

        self.__dict__.update(new_instance.__dict__)

    def get_url(self):
        return 'http://odnoklassniki.ru/%s' % self.slug

    @property
    def refresh_kwargs(self):
        raise NotImplementedError("Property %s.refresh_kwargs should be specified" % self.__class__.__name__)

    @property
    def slug(self):
        raise NotImplementedError("Property %s.slug should be specified" % self.__class__.__name__)


# class OdnoklassnikiIDModel(OdnoklassnikiModel):
#     class Meta:
#         abstract = True
#
#     remote_id = models.BigIntegerField(u'ID', help_text=u'Уникальный идентификатор', unique=True)
#
#     @property
#     def slug(self):
#         return self.slug_prefix + str(self.remote_id)
#
#     def save(self, *args, **kwargs):
#         '''
#         In case of IntegrityError, caused by `remote_id` field make substitution and save again
#         '''
#         try:
#             return super(OdnoklassnikiIDModel, self).save(*args, **kwargs)
#         except IntegrityError, e:
#             try:
#                 assert self.remote_id and 'remote_id' in unicode(e)
#                 instance = self.__class__.objects.get(remote_id=self.remote_id)
#                 self._substitute(instance)
#                 kwargs['force_insert'] = False
#                 return super(OdnoklassnikiIDModel, self).save(*args, **kwargs)
#             except (AssertionError, self.__class__.DoesNotExist):
#                 raise e


class OdnoklassnikiPKModel(OdnoklassnikiModel):

    class Meta:
        abstract = True

    id = models.BigIntegerField(u'ID', help_text=u'Уникальный идентификатор', primary_key=True)

    @property
    def slug(self):
        return '/'.join([self.slug_prefix, str(self.pk)])

# class OdnoklassnikiCRUDManager(models.Manager):
#
#     def create(self, *args, **kwargs):
#         instance = self.model(**kwargs)
#         instance.save()
#         return instance
#
#
# class OdnoklassnikiCRUDModel(models.Model):
#     class Meta:
#         abstract = True
#
# list of required number of fields for updating model remotely
#     fields_required_for_update = []
#
# flag should we update model remotely on save() and delete() methods
#     _commit_remote = True
#
#     archived = models.BooleanField(u'В архиве', default=False)
#
#     def __init__(self, *args, **kwargs):
#         self._commit_remote = kwargs.pop('commit_remote', self._commit_remote)
#         super(OdnoklassnikiCRUDModel, self).__init__(*args, **kwargs)
#
#     def delete(self, commit_remote=None, *args, **kwargs):
#         if not self.archived:
#             self.delete_remote(commit_remote)
#
#     def restore(self, commit_remote=None, *args, **kwargs):
#         if self.archived:
#             self.restore_remote(commit_remote)
#
#     def save(self, commit_remote=None, *args, **kwargs):
#         '''
#         Update remote version of object before saving if data is different
#         '''
#         commit_remote = commit_remote if commit_remote is not None else self._commit_remote
#         if commit_remote and COMMIT_REMOTE:
#             if not self.pk and not self.fetched:
#                 self.create_remote(**kwargs)
#             elif self.pk and self.fields_changed:
#                 self.update_remote(**kwargs)
#         super(OdnoklassnikiCRUDModel, self).save(*args, **kwargs)
#
#     def create_remote(self, **kwargs):
#         response = self.__class__.remote.api_call(
#                 method='create', **self.prepare_create_params(**kwargs))
#         self.remote_id = self.parse_remote_id_from_response(response)
#         log.info("Remote object %s was created successfully with ID %s" \
#                 % (self._meta.object_name, self.remote_id))
#
#     def update_remote(self, **kwargs):
#         params = self.prepare_update_params_distinct(**kwargs)
# sometimes response contains 1, sometimes remote_id
#         response = self.__class__.remote.api_call(method='update', **params)
#         if not response:
#             message = "Error response '%s' while saving remote %s with ID %s and data '%s'" \
#                     % (response, self._meta.object_name, self.remote_id, params)
#             log.error(message)
#             raise OdnoklassnikiContentError(message)
#         log.info("Remote object %s with ID=%s was updated with fields '%s' successfully" \
#                 % (self._meta.object_name, self.remote_id, params))
#
#     def delete_remote(self, commit_remote=None):
#         '''
#         Delete objects remotely and mark it archived localy
#         '''
#         commit_remote = commit_remote if commit_remote is not None else self._commit_remote
#         if commit_remote and self.remote_id:
#             params = self.prepare_delete_params()
#             success = self.__class__.remote.api_call(method='delete', **params)
#             model = self._meta.object_name
#             if not success:
#                 message = "Error response '%s' while deleting remote %s with ID %s" % (success, model, self.remote_id)
#                 log.error(message)
#                 raise OdnoklassnikiContentError(message)
#             log.info("Remote object %s with ID %s was deleted successfully" % (model, self.remote_id))
#
#         self.archived = True
#         self.save(commit_remote=False)
#
#     def restore_remote(self, commit_remote=None):
#         '''
#         Restore objects remotely and unmark it archived localy
#         '''
#         commit_remote = commit_remote if commit_remote is not None else self._commit_remote
#         if commit_remote and self.remote_id:
#             params = self.prepare_restore_params()
#             success = self.__class__.remote.api_call(method='restore', **params)
#             model = self._meta.object_name
#             if not success:
#                 message = "Error response '%s' while restoring remote %s with ID %s" % (success, model, self.remote_id)
#                 log.error(message)
#                 raise OdnoklassnikiContentError(message)
#             log.info("Remote object %s with ID %s was restored successfully" % (model, self.remote_id))
#
#         self.archived = False
#         self.save(commit_remote=False)
#
#     @property
#     def fields_changed(self):
#         old = self.__class__.objects.get(remote_id=self.remote_id)
#         return old.__dict__ != self.__dict__
#
#     def prepare_update_params_distinct(self, **kwargs):
#         '''
#         Return dict with distinct set of fields for update
#         '''
#         old = self.__class__.objects.get(remote_id=self.remote_id)
#         fields_new = self.prepare_update_params(**kwargs).items()
#         fields_old = old.prepare_update_params(**kwargs).items()
#         fields = dict(set(fields_new).difference(set(fields_old)))
#         fields.update(dict([(k,v) for k,v in fields_new if k in self.fields_required_for_update]))
#         return fields
#
#     @abstractmethod
#     def prepare_create_params(self, **kwargs):
#         """
#         Prepare params for remote create object.
#         Incoming params:
#             **kwargs - fields, which model instance hasn't
#
#         return {param_key: val, ....}
#         """
#         raise NotImplementedError
#
#     @abstractmethod
#     def prepare_update_params(self, **kwargs):
#         """
#         Prepare params for remote update object.
#         Incoming params:
#             **kwargs - fields, which model instance hasn't
#
#         return {param_key: val, ....}
#         """
#         raise NotImplementedError
#
#     @abstractmethod
#     def prepare_delete_params(self):
#         """
#         Prepar params for remote delete object.
#
#         return {param_key: val, ....}
#         """
#         raise NotImplementedError
#
#     @abstractmethod
#     def prepare_restore_params(self):
#         """
#         Prepar params for remote restore object.
#
#         return {param_key: val, ....}
#         """
#         return self.prepare_delete_params()
#
#     @abstractmethod
#     def parse_remote_id_from_response(self, response):
#         """
#         Extract remote_id from response from API create call.
#         Incoming param:
#             response - API crete call response
#
#         return 'some_id'
#         """
#         raise NotImplementedError
#
#     def check_remote_existance(self, *args, **kwargs):
#         self.refresh(*args, **kwargs)
#         if self.archived:
#             self.archive(commit_remote=False)
#             return False
#         else:
#             return True
