from __future__ import absolute_import

from otree.common import Currency
from django.db.models import BinaryField
import sys
import datetime
import inspect
import otree
import collections
import six
from django.utils.encoding import force_text
from collections import OrderedDict
from django.conf import settings
from django.db.models import Max, Count, Sum
from decimal import Decimal
import otree.constants_internal
from otree.models.participant import Participant
from otree.models.session import Session
from otree.models.subsession import BaseSubsession
from otree.models.group import BaseGroup
from otree.models.player import BasePlayer

from otree.models_concrete import (
    PageCompletion)
from otree.common_internal import get_models_module, app_name_format

if sys.version_info[0] == 2:
    import unicodecsv as csv
else:
    import csv


def inspect_field_names(Model):
    # filter out BinaryField, because it's not useful for CSV export or
    # live results. could be very big, and causes problems with utf-8 export

    # I tried .get_fields() instead of .fields, but that method returns
    # fields that cause problems, like saying group has an attribute 'player'
    return [f.name for f in Model._meta.fields
            if not isinstance(f, BinaryField)]


def get_field_names_for_live_update(Model):
    return _get_table_fields(Model, for_export=False)


def get_field_names_for_csv(Model):
    return _get_table_fields(Model, for_export=True)


def _get_table_fields(Model, for_export=False):

    if Model is Session:
        # only data export
        return [
            'code',
            'label',
            'experimenter_name',
            # not a field
            #'real_world_currency_per_point',
            'time_scheduled',
            'time_started',
            'mturk_HITId',
            'mturk_HITGroupId',
            # not a field
            #'participation_fee',
            'comment',
            'is_demo',
        ]

    if Model is Participant:
        if for_export:
            return [
                'id_in_session',
                'code',
                'label',
                '_is_bot',
                '_index_in_pages',
                '_max_page_index',
                '_current_app_name',
                '_round_number',
                '_current_page_name',
                'ip_address',
                'time_started',
                'exclude_from_data_analysis',
                'visited',
                'mturk_worker_id',
                'mturk_assignment_id',
            ]
        else:
            return [
                '_id_in_session',
                'code',
                'label',
                '_current_page',
                '_current_app_name',
                '_round_number',
                '_current_page_name',
                'status',
                '_last_page_timestamp',
            ]

    if issubclass(Model, BasePlayer):
        subclass_fields = [
            f for f in inspect_field_names(Model)
            if f not in inspect_field_names(BasePlayer)
            and f not in ['id', 'group', 'subsession']
            ]

        if for_export:
            return ['id_in_group'] + subclass_fields + ['payoff']
        else:
            return ['id_in_group', 'role'] + subclass_fields + ['payoff']

    if issubclass(Model, BaseGroup):
        subclass_fields = [
            f for f in inspect_field_names(Model)
            if f not in inspect_field_names(BaseGroup)
            and f not in ['id', 'subsession']
            ]

        return ['id_in_subsession'] + subclass_fields

    if issubclass(Model, BaseSubsession):
        subclass_fields = [
            f for f in inspect_field_names(Model)
            if f not in inspect_field_names(BaseGroup)
            and f != 'id'
            ]

        return ['round_number'] + subclass_fields


def sanitize_for_csv(value):
    if value is None:
        return ''
    if value is True:
        return '1'
    if value is False:
        return '0'
    value = force_text(value)
    return value.replace('\n', ' ').replace('\r', ' ')

def sanitize_for_live_update(value):
    if value is None:
        return ''
    if value is True:
        return 1
    if value is False:
        return 0
    return value


def get_rows_for_wide_csv():

    sessions = Session.objects.order_by('id').annotate(
        num_participants=Count('participant')).values()
    session_cache = {row['id']: row for row in sessions}

    participants = Participant.objects.order_by('id').values()

    payoff_cache = get_payoff_cache()
    payoff_plus_participation_fee_cache = get_payoff_plus_participation_fee_cache(payoff_cache)

    session_fields = get_field_names_for_csv(Session)
    participant_fields = get_field_names_for_csv(Participant)
    participant_fields += ['payoff', 'payoff_plus_participation_fee']
    header_row = ['participant.{}'.format(fname) for fname in participant_fields]
    header_row += ['session.{}'.format(fname) for fname in session_fields]
    rows = [header_row]
    for participant in participants:
        participant['payoff'] = payoff_cache[participant['id']]
        participant['payoff_plus_participation_fee'] = payoff_plus_participation_fee_cache[participant['id']]
        row = [sanitize_for_csv(participant[fname]) for fname in participant_fields]
        session = session_cache[participant['session_id']]
        row += [sanitize_for_csv(session[fname]) for fname in session_fields]
        rows.append(row)

    # heuristic to get the most relevant order of apps
    import json
    app_sequences = collections.Counter()
    for session in sessions:
        config = json.loads(session['config'])
        app_sequence = config['app_sequence']
        app_sequences[tuple(app_sequence)] += session['num_participants']
    most_common_app_sequence = app_sequences.most_common(1)[0][0]

    apps_not_in_popular_sequence = [
        app for app in settings.INSTALLED_OTREE_APPS
        if app not in most_common_app_sequence]

    order_of_apps = list(most_common_app_sequence) + apps_not_in_popular_sequence

    rounds_per_app = OrderedDict()
    for app_name in order_of_apps:
        models_module = get_models_module(app_name)
        agg_dict = models_module.Subsession.objects.all().aggregate(Max('round_number'))
        highest_round_number = agg_dict['round_number__max']

        if highest_round_number is not None:
            rounds_per_app[app_name] = highest_round_number
    for app_name in rounds_per_app:
        for round_number in range(1, rounds_per_app[app_name] + 1):
            new_rows = get_rows_for_wide_csv_round(app_name, round_number, sessions)
            for i in range(len(rows)):
                rows[i].extend(new_rows[i])
    return rows



def get_rows_for_wide_csv_round(app_name, round_number, sessions):

    models_module = otree.common_internal.get_models_module(app_name)
    Player = models_module.Player
    Group = models_module.Group
    Subsession = models_module.Subsession

    rows = []

    group_cache = {row['id']: row for row in Group.objects.values()}

    columns_for_models = {
        Model.__name__.lower(): get_field_names_for_csv(Model)
        for Model in [Player, Group, Subsession]
    }

    model_order = ['player', 'group', 'subsession']

    header_row = []
    for model_name in model_order:
        for colname in columns_for_models[model_name]:
            header_row.append('{}.{}.{}.{}'.format(
                app_name, round_number, model_name, colname))

    rows.append(header_row)
    empty_row = ['' for _ in range(len(header_row))]

    for session in sessions:
        subsession = Subsession.objects.filter(
            session_id=session['id'], round_number=round_number).values()
        if not subsession:
            subsession_rows = [empty_row for _ in range(session['num_participants'])]
        else:
            subsession = subsession[0]
            subsession_id = subsession['id']
            players = Player.objects.filter(subsession_id=subsession_id).order_by('id').values()

            subsession_rows = []

            for player in players:
                row = []
                all_objects = {
                    'player': player,
                    'group': group_cache[player['group_id']],
                    'subsession': subsession}

                for model_name in model_order:
                    for colname in columns_for_models[model_name]:
                        value = all_objects[model_name][colname]
                        row.append(sanitize_for_csv(value))
                subsession_rows.append(row)
        rows.extend(subsession_rows)
    return rows


def get_rows_for_csv(app_name):
    models_module = otree.common_internal.get_models_module(app_name)
    Player = models_module.Player
    Group = models_module.Group
    Subsession = models_module.Subsession

    columns_for_models = {
        Model.__name__.lower(): get_field_names_for_csv(Model)
        for Model in [Player, Group, Subsession, Participant, Session]
    }

    participant_ids = Player.objects.values_list('participant_id', flat=True)
    session_ids = Subsession.objects.values_list('session_id', flat=True)

    players = Player.objects.order_by('id').values()

    value_dicts = {
        'group': {row['id']: row for row in Group.objects.values()},
        'subsession': {row['id']: row for row in Subsession.objects.values()},
        'participant': {row['id']: row for row in
                        Participant.objects.filter(
                            id__in=participant_ids).values()},
        'session': {row['id']: row for row in
                    Session.objects.filter(id__in=session_ids).values()}
    }

    model_order = ['participant', 'player', 'group', 'subsession', 'session']

    # header row
    rows = [['{}.{}'.format(model_name, colname)
        for model_name in model_order
        for colname in columns_for_models[model_name]]]

    for player in players:
        row = []
        all_objects = {'player': player}
        for model_name in value_dicts:
            obj_id = player['{}_id'.format(model_name)]
            all_objects[model_name] = value_dicts[model_name][obj_id]

        for model_name in model_order:
            for colname in columns_for_models[model_name]:
                value = all_objects[model_name][colname]
                row.append(sanitize_for_csv(value))
        rows.append(row)

    return rows


def get_rows_for_live_update(app_name, subsession_pk):
    models_module = otree.common_internal.get_models_module(app_name)
    Player = models_module.Player
    Group = models_module.Group
    Subsession = models_module.Subsession


    columns_for_models = {
        Model.__name__.lower(): get_field_names_for_live_update(Model)
        for Model in [Player, Group, Subsession]
    }

    # we had a strange result on one person's heroku instance
    # where Meta.ordering on the Player was being ingnored
    # when you use a filter. So we add one explicitly.
    players = Player.objects.filter(
        subsession_id=subsession_pk).select_related(
        'group', 'subsession').order_by('pk')

    model_order = ['player', 'group', 'subsession']

    rows = []
    for player in players:
        row = []
        for model_name in model_order:
            if model_name == 'player':
                model_instance = player
            else:
                model_instance = getattr(player, model_name)

            for colname in columns_for_models[model_name]:

                attr = getattr(model_instance, colname, '')
                if isinstance(attr, collections.Callable):
                    if model_name == 'player' and colname == 'role' \
                            and model_instance.group is None:
                        attr = ''
                    else:
                        try:
                            attr = attr()
                        except:
                            attr = "(error)"
                row.append(sanitize_for_live_update(attr))
        rows.append(row)

    return columns_for_models, rows


def export_wide(fp, file_extension='csv'):
    rows = get_rows_for_wide_csv()
    if file_extension == 'xlsx':
        _export_xlsx(fp, rows)
    else:
        _export_csv(fp, rows)


def export_app(app_name, fp, file_extension='csv'):
    rows = get_rows_for_csv(app_name)
    if file_extension == 'xlsx':
        _export_xlsx(fp, rows)
    else:
        _export_csv(fp, rows)


def _export_csv(fp, rows):
    writer = csv.writer(fp)
    writer.writerows(rows)


def _export_xlsx(fp, rows):
    '''
    CSV often does not open properly in Excel, e.g. unicode
    '''
    import xlsxwriter
    workbook = xlsxwriter.Workbook(fp, {'in_memory': True})
    worksheet = workbook.add_worksheet()

    for row_num, row in enumerate(rows):
        for col_num, cell_value in enumerate(row):
            worksheet.write(row_num, col_num, cell_value)
    workbook.close()


def export_time_spent(fp):
    """Write the data of the timespent on each_page as csv into the file-like
    object
    """

    column_names = [
            'session_id',
            'participant__id_in_session',
            'participant__code',
            'page_index',
            'app_name',
            'page_name',
            'time_stamp',
            'seconds_on_page',
            'subsession_pk',
            'auto_submitted',
        ]

    rows = PageCompletion.objects.order_by(
        'session', 'participant', 'page_index'
    ).values_list(*column_names)
    writer = csv.writer(fp)
    writer.writerows([column_names])
    writer.writerows(rows)


def export_docs(fp, app_name):
    """Write the dcos of the given app name as csv into the file-like object

    """

    # generate doct_dict
    models_module = get_models_module(app_name)

    model_names = ["Participant", "Player", "Group", "Subsession", "Session"]
    line_break = '\r\n'

    def choices_readable(choices):
        lines = []
        for value, name in choices:
            # unicode() call is for lazy translation strings
            lines.append(u'{}: {}'.format(value, six.text_type(name)))
        return lines

    def generate_doc_dict():
        doc_dict = OrderedDict()

        data_types_readable = {
            'PositiveIntegerField': 'positive integer',
            'IntegerField': 'integer',
            'BooleanField': 'boolean',
            'CharField': 'text',
            'TextField': 'text',
            'FloatField': 'decimal',
            'DecimalField': 'decimal',
            'CurrencyField': 'currency'}

        for model_name in model_names:
            if model_name == 'Participant':
                Model = Participant
            elif model_name == 'Session':
                Model = Session
            else:
                Model = getattr(models_module, model_name)

            field_names = set(field.name for field in Model._meta.fields)

            members = get_field_names_for_csv(Model)
            doc_dict[model_name] = OrderedDict()

            for member_name in members:
                member = getattr(Model, member_name, None)
                doc_dict[model_name][member_name] = OrderedDict()
                if member_name == 'id':
                    doc_dict[model_name][member_name]['type'] = [
                        'positive integer']
                    doc_dict[model_name][member_name]['doc'] = ['Unique ID']
                elif member_name in field_names:
                    member = Model._meta.get_field_by_name(member_name)[0]

                    internal_type = member.get_internal_type()
                    data_type = data_types_readable.get(
                        internal_type, internal_type)

                    doc_dict[model_name][member_name]['type'] = [data_type]

                    # flag error if the model doesn't have a doc attribute,
                    # which it should unless the field is a 3rd party field
                    doc = getattr(member, 'doc', '[error]') or ''
                    doc_dict[model_name][member_name]['doc'] = [
                        line.strip() for line in doc.splitlines()
                        if line.strip()]

                    choices = getattr(member, 'choices', None)
                    if choices:
                        doc_dict[model_name][member_name]['choices'] = (
                            choices_readable(choices))
                elif isinstance(member, collections.Callable):
                    doc_dict[model_name][member_name]['doc'] = [
                        inspect.getdoc(member)]
        return doc_dict

    def docs_as_string(doc_dict):

        first_line = '{}: Documentation'.format(app_name_format(app_name))
        second_line = '*' * len(first_line)

        lines = [
            first_line, second_line, '',
            'Accessed: {}'.format(datetime.date.today().isoformat()), '']

        app_doc = getattr(models_module, 'doc', '')
        if app_doc:
            lines += [app_doc, '']

        for model_name in doc_dict:
            lines.append(model_name)

            for member in doc_dict[model_name]:
                lines.append('\t{}'.format(member))
                for info_type in doc_dict[model_name][member]:
                    lines.append('\t\t{}'.format(info_type))
                    for info_line in doc_dict[model_name][member][info_type]:
                        lines.append(u'{}{}'.format('\t' * 3, info_line))

        output = u'\n'.join(lines)
        return output.replace('\n', line_break).replace('\t', '    ')

    doc_dict = generate_doc_dict()
    doc = docs_as_string(doc_dict)
    fp.write(doc)


def get_payoff_cache():
    payoff_cache = collections.defaultdict(Decimal)

    for app_name in settings.INSTALLED_OTREE_APPS:
        models_module = get_models_module(app_name)
        Player = models_module.Player
        for d in Player.objects.values(
                'participant_id', 'session_id').annotate(Sum('payoff')):
            payoff_cache[d['participant_id']] += (d['payoff__sum'] or 0)

    return payoff_cache


def get_payoff_plus_participation_fee_cache(payoff_cache):
    # don't want to modify the input
    payoff_cache = payoff_cache.copy()

    for p_id in payoff_cache:
        # convert to Currency so we can call to_real_world_currency
        payoff_cache[p_id] = Currency(payoff_cache[p_id])

    participant_ids_to_session_ids = {
        p['id']: p['session_id'] for p in Participant.objects.values(
        'id', 'session_id')
    }

    sessions_cache = {s.id: s for s in Session.objects.all()}

    payoff_plus_participation_fee_cache = collections.defaultdict(Decimal)
    for p_id in payoff_cache:
        session_id = participant_ids_to_session_ids[p_id]
        session = sessions_cache[session_id]
        payoff = payoff_cache[p_id]
        payoff_plus_participation_fee = session._get_payoff_plus_participation_fee(payoff)
        payoff_plus_participation_fee_cache[p_id] = payoff_plus_participation_fee.to_number()

    return payoff_plus_participation_fee_cache

