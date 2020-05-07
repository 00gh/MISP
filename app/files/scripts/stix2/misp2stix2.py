#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#    Copyright (C) 2017-2018 CIRCL Computer Incident Response Center Luxembourg (smile gie)
#    Copyright (C) 2017-2018 Christian Studer
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

import sys, json, os, datetime
import pymisp
import re
import uuid
from stix2.base import STIXJSONEncoder
from stix2.exceptions import InvalidValueError, TLPMarkingDefinitionError
from stix2.properties import DictionaryProperty, ListProperty, StringProperty, TimestampProperty
from stix2.v20.common import MarkingDefinition, TLP_WHITE, TLP_GREEN, TLP_AMBER, TLP_RED
from stix2.v20.observables import WindowsPESection
from stix2.v20.sdo import AttackPattern, CourseOfAction, CustomObject, Identity, Indicator, IntrusionSet, Malware, ObservedData, Report, ThreatActor, Tool, Vulnerability
from stix2.v20.sro import Relationship
from misp2stix2_mapping import *
from collections import defaultdict
from copy import deepcopy

misp_hash_types = ("authentihash", "ssdeep", "imphash", "md5", "sha1", "sha224",
                   "sha256", "sha384", "sha512", "sha512/224","sha512/256","tlsh")
attack_pattern_galaxies_list = ('mitre-attack-pattern', 'mitre-enterprise-attack-attack-pattern',
                                'mitre-mobile-attack-attack-pattern', 'mitre-pre-attack-attack-pattern')
course_of_action_galaxies_list = ('mitre-course-of-action', 'mitre-enterprise-attack-course-of-action',
                                  'mitre-mobile-attack-course-of-action')
intrusion_set_galaxies_list = ('mitre-enterprise-attack-intrusion-set', 'mitre-mobile-attack-intrusion-set',
                               'mitre-pre-attack-intrusion-set', 'mitre-intrusion-set')
malware_galaxies_list = ('android', 'banker', 'stealer', 'backdoor', 'ransomware', 'mitre-malware',
                         'mitre-enterprise-attack-malware', 'mitre-mobile-attack-malware')
threat_actor_galaxies_list = ('threat-actor', 'microsoft-activity-group')
tool_galaxies_list = ('botnet', 'rat', 'exploit-kit', 'tds', 'tool', 'mitre-tool',
                      'mitre-enterprise-attack-tool', 'mitre-mobile-attack-tool')
_MISP_event_tags = ['Threat-Report', 'misp:tool="misp2stix2"']
_time_fields = {'indicator': ('valid_from', 'valid_until'),
                'observed-data': ('first_observed', 'last_observed')}

class StixBuilder():
    def __init__(self):
        self.orgs = []
        self.galaxies = []
        self.ids = {}
        self.custom_objects = {}

    def loadEvent(self, args):
        pathname = os.path.dirname(args[0])
        filename = os.path.join(pathname, args[1])
        with open(filename, 'rt', encoding='utf-8') as f:
            self.json_event = json.loads(f.read())
        self.filename = filename
        self.load_objects_mapping()
        self.load_galaxy_mapping()

    def buildEvent(self):
        try:
            self.initialize_misp_types()
            stix_packages = [sdo for event in self.json_event['response'] for sdo in self.handler(event['Event'])] if self.json_event.get('response') else self.handler(self.json_event['Event'])
            outputfile = "{}.out".format(self.filename)
            with open(outputfile, 'wt', encoding='utf-8') as f:
                f.write(json.dumps(stix_packages, cls=STIXJSONEncoder))
            print(json.dumps({'success': 1}))
        except Exception as e:
            print(json.dumps({'error': e.__str__()}))

    def eventReport(self):
        if not self.object_refs and self.links:
            self.add_custom(self.links.pop(0))
        external_refs = [self.__parse_link(link) for link in self.links]
        report_args = {'type': 'report', 'id': self.report_id, 'name': self.misp_event['info'],
                       'created_by_ref': self.identity_id, 'created': self.misp_event['date'],
                       'published': self.get_datetime_from_timestamp(self.misp_event['publish_timestamp']),
                       'modified': self.get_datetime_from_timestamp(self.misp_event['timestamp']),
                       'interoperability': True}
        labels = [tag for tag in _MISP_event_tags]
        if self.misp_event.get('Tag'):
            markings = []
            for tag in self.misp_event['Tag']:
                name = tag['name']
                markings.append(name) if name.startswith('tlp:') else labels.append(name)
            if markings:
                report_args['object_marking_refs'] = self.handle_tags(markings)
        report_args['labels'] = labels
        if external_refs:
            report_args['external_references'] = external_refs
        self.add_all_markings()
        self.add_all_relationships()
        report_args['object_refs'] = self.object_refs
        return Report(**report_args)

    @staticmethod
    def __parse_link(link):
        url = link['value']
        source = "url"
        if link.get('comment'):
            source += " - {}".format(link['comment'])
        return {'source_name': source, 'url': url}

    def add_all_markings(self):
        for marking in self.markings.values():
            self.append_object(marking)

    def add_all_relationships(self):
        for source, targets in self.relationships['defined'].items():
            if source.startswith('report'):
                continue
            source_type,_ = source.split('--')
            for target in targets:
                target_type,_ = target.split('--')
                try:
                    relation = relationshipsSpecifications[source_type][target_type]
                except KeyError:
                    # custom relationship (suggested by iglocska)
                    relation = "has"
                relationship = Relationship(source_ref=source, target_ref=target,
                                            relationship_type=relation, interoperability=True)
                self.append_object(relationship, id_mapping=False)
        for source_uuid, references in self.relationships['to_define'].items():
            for reference in references:
                target_uuid, relationship_type = reference
                try:
                    source = '{}--{}'.format(self.ids[source_uuid], source_uuid)
                    target = '{}--{}'.format(self.ids[target_uuid], target_uuid)
                except KeyError:
                    continue
                relationship = Relationship(source_ref=source, target_ref=target, interoperability=True,
                                            relationship_type=relationship_type.strip())
                self.append_object(relationship, id_mapping=False)

    def __set_identity(self):
        org = self.misp_event['Orgc']
        org_uuid = org['uuid']
        identity_id = 'identity--{}'.format(org_uuid)
        self.identity_id = identity_id
        if org_uuid not in self.orgs:
            identity = Identity(type="identity", id=identity_id, name=org["name"],
                                identity_class="organization", interoperability=True)
            self.SDOs.append(identity)
            self.orgs.append(org_uuid)
            return 1
        return 0

    def initialize_misp_types(self):
        describe_types_filename = os.path.join(pymisp.__path__[0], 'data/describeTypes.json')
        describe_types = open(describe_types_filename, 'r')
        categories_mapping = json.loads(describe_types.read())['result']['category_type_mappings']
        for category in categories_mapping:
            mispTypesMapping[category] = {'to_call': 'handle_person'}

    def handler(self, event):
        self.misp_event = event
        self.report_id = "report--{}".format(self.misp_event['uuid'])
        self.SDOs = []
        self.object_refs = []
        self.links = []
        self.markings = {}
        self.relationships = {'defined': defaultdict(list),
                              'to_define': {}}
        i = self.__set_identity()
        if self.misp_event.get('Attribute'):
            for attribute in self.misp_event['Attribute']:
                try:
                    getattr(self, mispTypesMapping[attribute['type']]['to_call'])(attribute)
                except KeyError:
                    self.add_custom(attribute)
        if self.misp_event.get('Object'):
            self.objects_to_parse = defaultdict(dict)
            for misp_object in self.misp_event['Object']:
                name = misp_object['name']
                if name == 'original-imported-file':
                    continue
                to_ids = self.fetch_ids_flag(misp_object['Attribute'])
                try:
                    getattr(self, objectsMapping[name]['to_call'])(misp_object, to_ids)
                except KeyError:
                    self.add_object_custom(misp_object, to_ids)
                if misp_object.get('ObjectReference'):
                    self.relationships['to_define'][misp_object['uuid']] = tuple((r['referenced_uuid'], r['relationship_type']) for r in misp_object['ObjectReference'])
            if self.objects_to_parse:
                self.resolve_objects2parse()
        if self.misp_event.get('Galaxy'):
            for galaxy in self.misp_event['Galaxy']:
                self.parse_galaxy(galaxy, self.report_id)
        report = self.eventReport()
        self.SDOs.insert(i, report)
        return self.SDOs

    def load_objects_mapping(self):
        self.objects_mapping = {
            'asn': {'observable': 'resolve_asn_observable',
                    'pattern': 'resolve_asn_pattern'},
            'credential': {'observable': 'resolve_credential_observable',
                           'pattern': 'resolve_credential_pattern'},
            'domain-ip': {'observable': 'resolve_domain_ip_observable',
                          'pattern': 'resolve_domain_ip_pattern'},
            'email': {'observable': 'resolve_email_object_observable',
                      'pattern': 'resolve_email_object_pattern'},
            'file': {'observable': 'resolve_file_observable',
                     'pattern': 'resolve_file_pattern'},
            'ip-port': {'observable': 'resolve_ip_port_observable',
                        'pattern': 'resolve_ip_port_pattern'},
            'network-connection': {'observable': 'resolve_network_connection_observable',
                                   'pattern': 'resolve_network_connection_pattern'},
            'network-socket': {'observable': 'resolve_network_socket_observable',
                               'pattern': 'resolve_network_socket_pattern'},
            'process': {'observable': 'resolve_process_observable',
                        'pattern': 'resolve_process_pattern'},
            'registry-key': {'observable': 'resolve_regkey_observable',
                             'pattern': 'resolve_regkey_pattern'},
            'stix2-pattern': {'pattern': 'resolve_stix2_pattern'},
            'url': {'observable': 'resolve_url_observable',
                    'pattern': 'resolve_url_pattern'},
            'user-account': {'observable': 'resolve_user_account_observable',
                             'pattern': 'resolve_user_account_pattern'},
            'x509': {'observable': 'resolve_x509_observable',
                     'pattern': 'resolve_x509_pattern'}
        }

    def load_galaxy_mapping(self):
        self.galaxies_mapping = {'branded-vulnerability': ['vulnerability', 'add_vulnerability_from_galaxy']}
        self.galaxies_mapping.update(dict.fromkeys(attack_pattern_galaxies_list, ['attack-pattern', 'add_attack_pattern']))
        self.galaxies_mapping.update(dict.fromkeys(course_of_action_galaxies_list, ['course-of-action', 'add_course_of_action']))
        self.galaxies_mapping.update(dict.fromkeys(intrusion_set_galaxies_list, ['intrusion-set', 'add_intrusion_set']))
        self.galaxies_mapping.update(dict.fromkeys(malware_galaxies_list, ['malware', 'add_malware']))
        self.galaxies_mapping.update(dict.fromkeys(threat_actor_galaxies_list, ['threat-actor', 'add_threat_actor']))
        self.galaxies_mapping.update(dict.fromkeys(tool_galaxies_list, ['tool', 'add_tool']))

    def get_object_by_uuid(self, uuid):
        for _object in self.misp_event['Object']:
            if _object.get('uuid') and _object['uuid'] == uuid:
                return _object
        raise Exception('Object with uuid {} does not exist in this event.'.format(uuid))

    def handle_person(self, attribute):
        if attribute['category'] == "Person":
            self.add_identity(attribute)
        else:
            self.add_custom(attribute)

    def handle_usual_type(self, attribute):
        try:
            if attribute['to_ids']:
                self.add_indicator(attribute)
            else:
                self.add_observed_data(attribute)
        except Exception:
            self.add_custom(attribute)

    def handle_usual_object_name(self, misp_object, to_ids):
        name = misp_object['name']
        if  name == 'file' and misp_object.get('ObjectReference'):
            for reference in misp_object['ObjectReference']:
                if reference['relationship_type'] in ('includes',  'included-in') and reference['Object']['name'] == "pe":
                    self.objects_to_parse[name][misp_object['uuid']] = to_ids, misp_object
                    return
        try:
            if to_ids or name == "stix2-pattern":
                self.add_object_indicator(misp_object)
            else:
                self.add_object_observable(misp_object)
        except Exception:
            self.add_object_custom(misp_object, to_ids)

    def handle_link(self, attribute):
        self.links.append(attribute)

    def populate_objects_to_parse(self, misp_object, to_ids):
        self.objects_to_parse[misp_object['name']][misp_object['uuid']] = to_ids, misp_object

    def resolve_objects2parse(self):
        for uuid, misp_object in self.objects_to_parse['file'].items():
            to_ids_file, file_object = misp_object
            file_id = "file--{}".format(file_object['uuid'])
            to_ids_list = [to_ids_file]
            for reference in file_object['ObjectReference']:
                if reference['relationship_type'] in ("includes", "included-in") and reference['Object']['name'] == "pe":
                    pe_uuid = reference['referenced_uuid']
                    break
            to_ids_pe, pe_object = self.objects_to_parse['pe'][pe_uuid]
            to_ids_list.append(to_ids_pe)
            sections = []
            for reference in pe_object['ObjectReference']:
                if reference['Object']['name'] == "pe-section" and reference['referenced_uuid'] in self.objects_to_parse['pe-section']:
                    to_ids_section, section_object = self.objects_to_parse['pe-section'][reference['referenced_uuid']]
                    to_ids_list.append(to_ids_section)
                    sections.append(section_object)
            if True in to_ids_list:
                patterns = self.resolve_file_pattern(file_object['Attribute'], file_id)
                patterns.extend(self.parse_pe_extensions_pattern(pe_object, sections))
                self.add_object_indicator(file_object, pattern_arg=f"[{' AND '.join(patterns)}]")
            else:
                observable = self.resolve_file_observable(file_object['Attribute'], file_id)
                key = '0' if len(observable) == 1 else self._fetch_file_observable(observable)
                pe_type = self._get_pe_type_from_filename(observable[key])
                observable[key]['extensions'] = self.parse_pe_extensions_observable(pe_object, sections, pe_type)
                self.add_object_observable(file_object, observable_arg=observable)

    @staticmethod
    def _create_pe_type_test(observable, extension):
        return [
            ('name' in observable and observable['name'].endswith(f'.{extension}')),
            ('mime_type' in observable and re.compile(".* .+{0}.+ .*|.* {0} .*".format(extension)).match(observable['mime_type'].lower()))]

    def _get_pe_type_from_filename(self, observable):
        for extension in ('exe', 'dll'):
            if any(self._create_pe_type_test(observable, extension)):
                return extension
        return 'sys'

    @staticmethod
    def _fetch_file_observable(observable_objects):
        for key, observable in observable_objects.items():
            if observable['type'] == 'file':
                return key
        return '0'

    def parse_pe_extensions_observable(self, pe_object, sections, pe_type):
        extension = defaultdict(list)
        extension['pe_type'] = pe_type
        for attribute in pe_object['Attribute']:
            try:
                extension[peMapping[attribute['object_relation']]] = attribute['value']
            except KeyError:
                extension["x_misp_{}_{}".format(attribute['type'], attribute['object_relation'].replace('-', '_'))] = attribute['value']
        for section in sections:
            d_section = defaultdict(dict)
            for attribute in section['Attribute']:
                relation = attribute['object_relation']
                if relation in misp_hash_types:
                    d_section['hashes'][relation] = attribute['value']
                else:
                    try:
                        d_section[peSectionMapping[relation]] = attribute['value']
                    except KeyError:
                        continue
            if 'name' not in d_section:
                d_section['name'] = 'Section {}'.format(sections.index(section))
            extension['sections'].append(WindowsPESection(**d_section))
        if len(sections) != int(extension['number_of_sections']):
            extension['number_of_sections'] = str(len(sections))
        return {"windows-pebinary-ext": extension}

    def parse_pe_extensions_pattern(self, pe_object, sections):
        pattern = []
        mapping = objectsMapping['file']['pattern']
        pe_mapping = "extensions.'windows-pebinary-ext'"
        for attribute in pe_object['Attribute']:
            try:
                stix_type = f"{pe_mapping}.{peMapping[attribute['object_relation']]}"
            except KeyError:
                stix_type = f"{pe_mapping}.x_misp_{attribute['type']}_{attribute['object_relation'].replace('-', '_')}"
            pattern.append(mapping.format(stix_type, attribute['value']))
        n_section = 0
        for section in sections:
            section_mapping = f"{pe_mapping}.sections[{str(n_section)}]"
            for attribute in section['Attribute']:
                relation = attribute['object_relation']
                if relation in misp_hash_types:
                    stix_type = "{}.hashes.'{}'".format(section_mapping, relation)
                    pattern.append(mapping.format(stix_type, attribute['value']))
                else:
                    try:
                        stix_type = "{}.{}".format(section_mapping, peSectionMapping[relation])
                        pattern.append(mapping.format(stix_type, attribute['value']))
                    except KeyError:
                        continue
            n_section += 1
        return pattern

    def parse_galaxies(self, galaxies, source_id):
        for galaxy in galaxies:
            self.parse_galaxy(galaxy, source_id)

    def parse_galaxy(self, galaxy, source_id):
        galaxy_type = galaxy.get('type')
        galaxy_uuid = galaxy['GalaxyCluster'][0]['collection_uuid']
        try:
            stix_type, to_call = self.galaxies_mapping[galaxy_type]
        except Exception:
            return
        if galaxy_uuid not in self.galaxies:
            getattr(self, to_call)(galaxy)
            self.galaxies.append(galaxy_uuid)
        self.relationships['defined'][source_id].append("{}--{}".format(stix_type, galaxy_uuid))

    def generate_galaxy_args(self, galaxy, b_killchain, b_alias, sdo_type):
        cluster = galaxy['GalaxyCluster'][0]
        try:
            cluster_uuid = cluster['collection_uuid']
        except KeyError:
            cluster_uuid = cluster['uuid']
        sdo_id = "{}--{}".format(sdo_type, cluster_uuid)
        description = "{} | {}".format(galaxy['description'], cluster['description'])
        labels = ['misp:name=\"{}\"'.format(galaxy['name'])]
        sdo_args = {'id': sdo_id, 'type': sdo_type, 'created': self.misp_event['date'],
                    'modified': self.get_datetime_from_timestamp(self.misp_event['timestamp']),
                    'name': cluster['value'], 'description': description, 'interoperability': True}
        if b_killchain:
            killchain = [{'kill_chain_name': 'misp-category',
                          'phase_name': galaxy['type']}]
            sdo_args['kill_chain_phases'] = killchain
        if cluster['tag_name']:
            labels.append(cluster.get('tag_name'))
        meta = cluster.get('meta')
        if 'synonyms' in meta and b_alias:
            aliases = []
            for a in meta['synonyms']:
                aliases.append(a)
            sdo_args['aliases'] = aliases
        sdo_args['labels'] = labels
        return sdo_args

    def add_attack_pattern(self, galaxy):
        a_p_args = self.generate_galaxy_args(galaxy, True, False, 'attack-pattern')
        a_p_args['created_by_ref'] = self.identity_id
        attack_pattern = AttackPattern(**a_p_args)
        self.append_object(attack_pattern)

    def add_attack_pattern_object(self, misp_object, to_ids):
        attack_pattern_args = {'id': f'attack-pattern--{misp_object["uuid"]}', 'type': 'attack-pattern',
                               'created_by_ref': self.identity_id, 'interoperability': True}
        attack_pattern_args.update(self.parse_attack_pattern_fields(misp_object['Attribute']))
        attack_pattern_args['labels'] = self.create_object_labels(misp_object['name'], misp_object['meta-category'], to_ids)
        attack_pattern = AttackPattern(**attack_pattern_args)
        self.append_object(attack_pattern)

    def add_course_of_action(self, misp_object):
        coa_args= self.generate_galaxy_args(misp_object, False, False, 'course-of-action')
        self.add_coa_stix_object(coa_args)

    def add_course_of_action_from_object(self, misp_object, to_ids):
        coa_id = 'course-of-action--{}'.format(misp_object['uuid'])
        coa_args = {'id': coa_id, 'type': 'course-of-action', 'created_by_ref': self.identity_id}
        coa_args['labels'] = self.create_object_labels(misp_object['name'], misp_object['meta-category'], to_ids)
        for attribute in misp_object['Attribute']:
            self.parse_galaxies(attribute['Galaxy'], coa_id)
            relation = attribute['object_relation']
            if relation in ('name', 'description'):
                coa_args[relation] = attribute['value']
            else:
                coa_args[f'x_misp_{attribute["type"]}_{relation}'] = attribute['value']
        if not 'name' in coa_args:
            return
        self.add_coa_stix_object(coa_args)

    def add_coa_stix_object(self, coa_args):
        coa_args['created_by_ref'] = self.identity_id
        course_of_action = CourseOfAction(**coa_args, allow_custom=True)
        self.append_object(course_of_action)

    def add_custom(self, attribute):
        attribute_type = attribute['type'].replace('|', '-').replace(' ', '-').lower()
        custom_object_id = "x-misp-object-{}--{}".format(attribute_type, attribute['uuid'])
        custom_object_type = "x-misp-object-{}".format(attribute_type)
        labels, markings = self.create_labels(attribute)
        timestamp = self.get_datetime_from_timestamp(attribute['timestamp'])
        custom_object_args = {'id': custom_object_id, 'x_misp_category': attribute['category'],
                              'created': timestamp, 'modified': timestamp, 'labels': labels,
                              'x_misp_value': attribute['value'], 'created_by_ref': self.identity_id}
        if attribute.get('comment'):
            custom_object_args['x_misp_comment'] = attribute['comment']
        if markings:
            markings = self.handle_tags(markings)
            custom_object_args['object_marking_refs'] = markings
        if custom_object_type not in self.custom_objects:
            @CustomObject(custom_object_type, [
                ('id', StringProperty(required=True)),
                ('labels', ListProperty(labels, required=True)),
                ('x_misp_value', StringProperty(required=True)),
                ('created', TimestampProperty(required=True, precision='millisecond')),
                ('modified', TimestampProperty(required=True, precision='millisecond')),
                ('created_by_ref', StringProperty(required=True)),
                ('object_marking_refs', ListProperty(markings)),
                ('x_misp_comment', StringProperty()),
                ('x_misp_category', StringProperty())
            ])
            class Custom(object):
                def __init__(self, **kwargs):
                    return
            self.custom_objects[custom_object_type] = Custom
        else:
            Custom = self.custom_objects[custom_object_type]
        custom_object = Custom(**custom_object_args)
        self.append_object(custom_object)

    def add_identity(self, attribute):
        identity_id = "identity--{}".format(attribute['uuid'])
        name = attribute['value']
        labels, markings = self.create_labels(attribute)
        identity_args = {'id': identity_id,  'type': identity, 'name': name, 'labels': labels,
                          'identity_class': 'individual', 'created_by_ref': self.identity_id,
                          'interoperability': True}
        if attribute.get('comment'):
            identity_args['description'] = attribute['comment']
        if markings:
            identity_args['object_marking_refs'] = self.handle_tags(markings)
        identity = Identity(**identity_args)
        self.append_object(identity)

    def add_indicator(self, attribute):
        attribute_type = attribute['type']
        indicator_id = "indicator--{}".format(attribute['uuid'])
        self.parse_galaxies(attribute['Galaxy'], indicator_id)
        category = attribute['category']
        killchain = self.create_killchain(category)
        labels, markings = self.create_labels(attribute)
        attribute_value = attribute['value'] if attribute_type != "AS" else self.define_attribute_value(attribute['value'], attribute['comment'])
        pattern = mispTypesMapping[attribute_type]['pattern'](attribute_type, attribute_value, attribute['data']) if attribute.get('data') else self.define_pattern(attribute_type, attribute_value)
        timestamp = self.get_datetime_from_timestamp(attribute['timestamp'])
        indicator_args = {'id': indicator_id, 'type': 'indicator', 'labels': labels,
                          'kill_chain_phases': killchain, 'created_by_ref': self.identity_id,
                          'pattern': pattern, 'interoperability': True}
        indicator_args.update(self.handle_time_fields(attribute, timestamp, 'indicator'))
        if attribute.get('comment'):
            indicator_args['description'] = attribute['comment']
        if markings:
            indicator_args['object_marking_refs'] = self.handle_tags(markings)
        indicator = Indicator(**indicator_args)
        self.append_object(indicator)

    def add_intrusion_set(self, galaxy):
        i_s_args = self.generate_galaxy_args(galaxy, False, True, 'intrusion-set')
        i_s_args['created_by_ref'] = self.identity_id
        intrusion_set = IntrusionSet(**i_s_args)
        self.append_object(intrusion_set)

    def add_malware(self, galaxy):
        malware_args= self.generate_galaxy_args(galaxy, True, False, 'malware')
        malware_args['created_by_ref'] = self.identity_id
        malware = Malware(**malware_args)
        self.append_object(malware)

    def add_observed_data(self, attribute):
        attribute_type = attribute['type']
        observed_data_id = "observed-data--{}".format(attribute['uuid'])
        self.parse_galaxies(attribute['Galaxy'], observed_data_id)
        timestamp = self.get_datetime_from_timestamp(attribute['timestamp'])
        labels, markings = self.create_labels(attribute)
        attribute_value = attribute['value'] if attribute_type != "AS" else self.define_attribute_value(attribute['value'], attribute['comment'])
        observable = mispTypesMapping[attribute_type]['observable'](attribute_type, attribute_value, attribute['data']) if attribute.get('data') else self.define_observable(attribute_type, attribute_value)
        observed_data_args = {'id': observed_data_id, 'type': 'observed-data', 'number_observed': 1,
                              'objects': observable, 'created_by_ref': self.identity_id,
                              'labels': labels, 'interoperability': True}
        observed_data_args.update(self.handle_time_fields(attribute, timestamp, 'observed-data'))
        if markings:
            observed_data_args['object_marking_refs'] = self.handle_tags(markings)
        observed_data = ObservedData(**observed_data_args)
        self.append_object(observed_data)

    def add_threat_actor(self, galaxy):
        t_a_args = self.generate_galaxy_args(galaxy, False, True, 'threat-actor')
        t_a_args['created_by_ref'] = self.identity_id
        threat_actor = ThreatActor(**t_a_args)
        self.append_object(threat_actor)

    def add_tool(self, galaxy):
        tool_args = self.generate_galaxy_args(galaxy, True, False, 'tool')
        tool_args['created_by_ref'] = self.identity_id
        tool = Tool(**tool_args)
        self.append_object(tool)

    def add_vulnerability(self, attribute):
        vulnerability_id = "vulnerability--{}".format(attribute['uuid'])
        name = attribute['value']
        vulnerability_data = [mispTypesMapping['vulnerability']['vulnerability_args'](name)]
        labels, markings = self.create_labels(attribute)
        vulnerability_args = {'id': vulnerability_id, 'type': 'vulnerability',
                              'name': name, 'external_references': vulnerability_data,
                              'created_by_ref': self.identity_id, 'labels': labels,
                              'interoperability': True}
        if markings:
            vulnerability_args['object_marking_refs'] = self.handle_tags(markings)
        vulnerability = Vulnerability(**vulnerability_args)
        self.append_object(vulnerability)

    def add_vulnerability_from_galaxy(self, attribute):
        vulnerability_id = "vulnerability--{}".format(attribute['uuid'])
        cluster = attribute['GalaxyCluster'][0]
        name = cluster['value']
        if cluster['meta'] and cluster['meta']['aliases']:
            vulnerability_data = [mispTypesMapping['vulnerability']['vulnerability_args'](alias) for alias in cluster['meta']['aliases']]
        else:
            vulnerability_data = [mispTypesMapping['vulnerability']['vulnerability_args'](name)]
        labels = ['misp:type=\"{}\"'.format(attribute.get('type'))]
        if cluster['tag_name']:
            labels.append(cluster['tag_name'])
        description = "{} | {}".format(attribute.get('description'), cluster.get('description'))
        vulnerability_args = {'id': vulnerability_id, 'type': 'vulnerability',
                              'name': name, 'external_references': vulnerability_data,
                              'created_by_ref': self.identity_id, 'labels': labels,
                              'description': description, 'interoperability': True}
        vulnerability = Vulnerability(**vulnerability_args)
        self.append_object(vulnerability)

    def add_object_custom(self, misp_object, to_ids):
        name = misp_object['name']
        custom_object_id = 'x-misp-object-{}--{}'.format(name, misp_object['uuid'])
        custom_object_type = 'x-misp-object-{}'.format(name)
        category = misp_object.get('meta-category')
        labels = self.create_object_labels(name, category, to_ids)
        values = self.fetch_custom_values(misp_object['Attribute'], custom_object_id)
        timestamp = self.get_datetime_from_timestamp(misp_object['timestamp'])
        custom_object_args = {'id': custom_object_id, 'x_misp_values': values,
                              'created': timestamp, 'modified': timestamp, 'labels': labels,
                              'x_misp_category': category, 'created_by_ref': self.identity_id}
        if hasattr(misp_object, 'comment') and misp_object.get('comment'):
            custom_object_args['x_misp_comment'] = misp_object['comment']
        if custom_object_type not in self.custom_objects:
            @CustomObject(custom_object_type, [
                ('id', StringProperty(required=True)),
                ('labels', ListProperty(labels, required=True)),
                ('x_misp_value', StringProperty(required=True)),
                ('created', TimestampProperty(required=True, precision='millisecond')),
                ('modified', TimestampProperty(required=True, precision='millisecond')),
                ('created_by_ref', StringProperty(required=True)),
                ('x_misp_comment', StringProperty()),
                ('x_misp_category', StringProperty())
            ])
            class Custom(object):
                def __init__(self, **kwargs):
                    return
            self.custom_objects[custom_object_type] = Custom
        else:
            Custom = self.custom_objects[custom_object_type]
        custom_object = Custom(**custom_object_args)
        self.append_object(custom_object)

    def add_object_indicator(self, misp_object, pattern_arg=None):
        indicator_id = 'indicator--{}'.format(misp_object['uuid'])
        if pattern_arg:
            name = 'WindowsPEBinaryFile'
            pattern = pattern_arg
        else:
            name = misp_object['name']
            pattern = f"[{' AND '.join(getattr(self, self.objects_mapping[name]['pattern'])(misp_object['Attribute'], indicator_id))}]"
        category = misp_object.get('meta-category')
        killchain = self.create_killchain(category)
        labels = self.create_object_labels(name, category, True)
        timestamp = self.get_datetime_from_timestamp(misp_object['timestamp'])
        indicator_args = {'id': indicator_id, 'type': 'indicator',
                          'labels': labels, 'pattern': pattern,
                          'description': misp_object['description'], 'allow_custom': True,
                          'kill_chain_phases': killchain, 'interoperability': True,
                          'created_by_ref': self.identity_id}
        indicator_args.update(self.handle_time_fields(misp_object, timestamp, 'indicator'))
        indicator = Indicator(**indicator_args)
        self.append_object(indicator)

    def add_object_observable(self, misp_object, observable_arg=None):
        observed_data_id = 'observed-data--{}'.format(misp_object['uuid'])
        if observable_arg:
            name = 'WindowsPEBinaryFile'
            observable_objects = observable_arg
        else:
            name = misp_object['name']
            observable_objects = getattr(self, self.objects_mapping[name]['observable'])(misp_object['Attribute'], observed_data_id)
        category = misp_object.get('meta-category')
        labels = self.create_object_labels(name, category, False)
        timestamp = self.get_datetime_from_timestamp(misp_object['timestamp'])
        observed_data_args = {'id': observed_data_id, 'type': 'observed-data', 'labels': labels,
                              'number_observed': 1, 'objects': observable_objects, 'allow_custom': True,
                              'created_by_ref': self.identity_id, 'interoperability': True}
        observed_data_args.update(self.handle_time_fields(misp_object, timestamp, 'observed-data'))
        try:
            observed_data = ObservedData(**observed_data_args)
        except InvalidValueError:
            observed_data = self.fix_enumeration_issues(name, observed_data_args)
        self.append_object(observed_data)

    @staticmethod
    def fix_enumeration_issues(name, args):
        if name == 'network-socket':
            socket_args = deepcopy(args)
            n = None
            for index, observable_object in socket_args['objects'].items():
                if observable_object['type'] == 'network-traffic':
                    n = index
                    break
            if n is not None:
                extension = socket_args['objects'][n]['extensions']['socket-ext']
                feature = 'address_family'
                if feature not in extension:
                    extension[feature] = 'AF_UNSPEC'
                elif extension[feature] not in SocketExt._properties[feature].allowed:
                    extension[f'x_misp_text_{feature}'] = extension[feature]
                    extension[feature] = 'AF_UNSPEC'
                feature = 'protocol_family'
                if feature in extension and extension[feature] not in SocketExt._properties[feature].allowed:
                    extension['x_misp_text_domain_family'] = extension.pop(feature)
            return ObservedData(**socket_args)
            # If there is still an issue at this point, well at least we tried to fix it
        return ObservedData(**args)

    def add_object_vulnerability(self, misp_object, to_ids):
        vulnerability_id = 'vulnerability--{}'.format(misp_object['uuid'])
        vulnerability_args = {'id': vulnerability_id, 'type': 'vulnerability',
                              'created_by_ref': self.identity_id, 'interoperability': True}
        vulnerability_args.update(self.parse_vulnerability_fields(misp_object['Attribute']))
        vulnerability_args['labels'] = self.create_object_labels(misp_object['name'], misp_object['meta-category'], to_ids)
        vulnerability = Vulnerability(**vulnerability_args)
        self.append_object(vulnerability)

    def append_object(self, stix_object, id_mapping=True):
        self.SDOs.append(stix_object)
        self.object_refs.append(stix_object.id)
        if id_mapping:
            object_type, uuid = stix_object.id.split('--')
            self.ids[uuid] = object_type

    @staticmethod
    def create_killchain(category):
        return [{'kill_chain_name': 'misp-category', 'phase_name': category}]

    @staticmethod
    def create_labels(attribute):
        labels = ['misp:type="{}"'.format(attribute['type']),
                  'misp:category="{}"'.format(attribute['category']),
                  'misp:to_ids="{}"'.format(attribute['to_ids'])]
        markings = []
        if attribute.get('Tag'):
            for tag in attribute['Tag']:
                name = tag['name']
                markings.append(name) if name.startswith('tlp:') else labels.append(name)
        return labels, markings

    @staticmethod
    def create_object_labels(name, category, to_ids):
        return ['misp:type="{}"'.format(name),
                'misp:category="{}"'.format(category),
                'misp:to_ids="{}"'.format(to_ids),
                'from_object']

    def create_marking(self, tag):
        if tag in tlp_markings:
            marking_definition = globals()[tlp_markings[tag]]
            self.markings[tag] = marking_definition
            return marking_definition.id
        marking_id = 'marking-definition--%s' % uuid.uuid4()
        definition_type, definition = tag.split(':')
        marking_definition = {'type': 'marking-definition', 'id': marking_id, 'definition_type': definition_type,
                              'definition': {definition_type: definition}}
        try:
            self.markings[tag] = MarkingDefinition(**marking_definition)
        except (TLPMarkingDefinitionError, ValueError):
            return
        return marking_id

    @staticmethod
    def _parse_tag(namespace, predicate):
        if '=' not in predicate:
            return "{} = {}".format(namespace, predicate)
        predicate, value = predicate.split('=')
        return "({}) {} = {}".format(namespace, predicate, value.strip('"'))

    @staticmethod
    def define_observable(attribute_type, attribute_value):
        if attribute_type == 'malware-sample':
            return mispTypesMapping[attribute_type]['observable']('filename|md5', attribute_value)
        observable = mispTypesMapping[attribute_type]['observable'](attribute_type, attribute_value)
        if attribute_type == 'port':
            observable['0']['protocols'].append(defineProtocols[attribute_value] if attribute_value in defineProtocols else "tcp")
        return observable

    @staticmethod
    def define_pattern(attribute_type, attribute_value):
        attribute_value = attribute_value.replace("'", '##APOSTROPHE##').replace('"', '##QUOTE##') if isinstance(attribute_value, str) else attribute_value
        if attribute_type == 'malware-sample':
            return mispTypesMapping[attribute_type]['pattern']('filename|md5', attribute_value)
        return mispTypesMapping[attribute_type]['pattern'](attribute_type, attribute_value)

    def fetch_custom_values(self, attributes, object_id):
        values = defaultdict(list)
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            attribute_type = '{}_{}'.format(attribute['type'], attribute['object_relation'])
            values[attribute_type].append(attribute['value'])
        return {attribute_type: value[0] if len(value) == 1 else value for attribute_type, value in values.items()}

    @staticmethod
    def fetch_ids_flag(attributes):
        for attribute in attributes:
            if attribute['to_ids']:
                return True
        return False

    def handle_tags(self, tags):
        marking_ids = []
        for tag in tags:
            marking_id = self.markings[tag]['id'] if tag in self.markings else self.create_marking(tag)
            if marking_id:
                marking_ids.append(marking_id)
        return marking_ids

    def parse_attack_pattern_fields(self, attributes):
        attack_pattern = {}
        weaknesses = []
        references = []
        for attribute in attributes:
            relation = attribute['object_relation']
            if relation in attackPatternObjectMapping:
                attack_pattern[attackPatternObjectMapping[relation]] = attribute['value']
            else:
                if relation in ('id', 'references'):
                    references.append(self._parse_attack_pattern_reference(attribute))
                elif relation == 'related-weakness':
                    weaknesses.append(attribute['value'])
                else:
                    attack_pattern[f"x_misp_{attribute['type']}_{relation.replace('-', '_')}"] = attribute['value']
                    attack_pattern['allow_custom'] = True
        if references:
            attack_pattern['external_references'] = references
        if weaknesses:
            attack_pattern['x_misp_weakness_related_weakness'] = weaknesses[0] if len(weaknesses) == 1 else weaknesses
        return attack_pattern

    @staticmethod
    def _parse_attack_pattern_reference(attribute):
        object_relation = attribute['object_relation']
        source_name, key = attack_pattern_reference_mapping[object_relation]
        value = attribute['value']
        if object_relation == 'id' and 'CAPEC' not in value:
            value = f'CAPEC-{value}'
        return {'source_name': source_name, key: value}

    @staticmethod
    def parse_vulnerability_fields(attributes):
        vulnerability = {}
        references = []
        custom_args = defaultdict(list)
        for attribute in attributes:
            relation = attribute['object_relation']
            if relation in vulnerabilityMapping:
                vulnerability[vulnerabilityMapping[relation]] = attribute['value']
            else:
                if relation == 'references':
                    references.append({'source_name': 'url', 'url': attribute['value']})
                else:
                    custom_args[f"x_misp_{attribute['type']}_{relation.replace('-', '_')}"].append(attribute['value'])
                    vulnerability['allow_custom'] = True
        if 'name' in vulnerability:
            references.append({'source_name': 'cve', 'external_id': vulnerability['name']})
        if references:
            vulnerability['external_references'] = references
        if custom_args:
            vulnerability.update({key: value[0] if len(value) == 1 else value for key, value in custom_args.items()})
        return vulnerability

    def resolve_asn_observable(self, attributes, object_id):
        asn = objectsMapping['asn']['observable']
        observable = {}
        object_num = 0
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            relation = attribute['object_relation']
            try:
                stix_type = asnObjectMapping[relation]
            except KeyError:
                stix_type = "x_misp_{}_{}".format(attribute['type'], relation)
            attribute_value = attribute['value']
            if relation == "subnet-announced":
                observable[str(object_num)] = {'type': define_address_type(attribute_value), 'value': attribute_value}
                object_num += 1
            else:
                asn[stix_type] = int(attribute_value[2:]) if (stix_type == 'number' and attribute_value.startswith("AS")) else attribute_value
        observable[str(object_num)] = asn
        for n in range(object_num):
            observable[str(n)]['belongs_to_refs'] = [str(object_num)]
        return observable

    def resolve_asn_pattern(self, attributes, object_id):
        mapping = objectsMapping['asn']['pattern']
        pattern = []
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            relation = attribute['object_relation']
            try:
                stix_type = asnObjectMapping[relation]
            except KeyError:
                stix_type = "'x_misp_{}_{}'".format(attribute['type'], relation)
            attribute_value = attribute['value']
            if relation == "subnet-announced":
                pattern.append("{0}:{1} = '{2}'".format(define_address_type(attribute_value), stix_type, attribute_value))
            else:
                pattern.append(mapping.format(stix_type, attribute_value))
        return pattern

    def resolve_credential_observable(self, attributes, object_id):
        user_account = objectsMapping['credential']['observable']
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            relation = attribute['object_relation']
            try:
                stix_type = credentialObjectMapping[relation]
            except KeyError:
                stix_type = "x_misp_{}_{}".format(attribute['type'], relation)
            user_account[stix_type] = attribute['value']
        return {'0': user_account}

    def resolve_credential_pattern(self, attributes, object_id):
        mapping = objectsMapping['credential']['pattern']
        pattern = []
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            relation = attribute['object_relation']
            try:
                stix_type = credentialObjectMapping[relation]
            except KeyError:
                stix_type = "x_misp_{}_{}".format(attribute['type'], relation)
            pattern.append(mapping.format(stix_type, attribute['value']))
        return pattern

    def resolve_domain_ip_observable(self, attributes, object_id):
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            if attribute['type'] == 'ip-dst':
                ip_value = attribute['value']
            elif attribute['type'] == 'domain':
                domain_value = attribute['value']
        domain_ip_value = "{}|{}".format(domain_value, ip_value)
        return mispTypesMapping['domain|ip']['observable']('', domain_ip_value)

    def resolve_domain_ip_pattern(self, attributes, object_id):
        mapping = objectsMapping['domain-ip']['pattern']
        pattern = []
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            try:
                stix_type = domainIpObjectMapping[attribute['type']]
            except KeyError:
                continue
            pattern.append(mapping.format(stix_type, attribute['value']))
        return pattern

    def resolve_email_object_observable(self, attributes, object_id):
        observable = {}
        message = defaultdict(list)
        additional_header = {}
        object_num = 0
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            relation = attribute['object_relation']
            attribute_value = attribute['value']
            try:
                mapping = emailObjectMapping[relation]['stix_type']
                if relation in ('from', 'to', 'cc'):
                    object_str = str(object_num)
                    observable[object_str] = {'type': 'email-addr', 'value': attribute_value}
                    if relation == 'from':
                        message[mapping] = object_str
                    else:
                        message[mapping].append(object_str)
                    object_num += 1
                elif relation in ('attachment', 'screenshot'):
                    object_str = str(object_num)
                    body = {"content_disposition": "{}; filename='{}'".format(relation, attribute_value),
                            "body_raw_ref": object_str}
                    message['body_multipart'].append(body)
                    observable[object_str] = {'type': 'artifact', 'payload_bin': attribute['data']} if 'data' in attribute and attribute['data'] else {'type': 'file', 'name': attribute_value}
                    object_num += 1
                elif relation in ('x-mailer', 'reply-to'):
                    key = '-'.join([part.capitalize() for part in relation.split('-')])
                    additional_header[key] = attribute_value
                else:
                    message[mapping] = attribute_value
            except Exception:
                mapping = "x_misp_{}_{}".format(attribute['type'], relation)
                message[mapping] = {'value': attribute_value, 'data': attribute['data']} if relation == 'eml' else attribute_value
        if additional_header:
            message['additional_header_fields'] = additional_header
        message['type'] = 'email-message'
        if 'body_multipart' in message:
            message['is_multipart'] = True
        else:
            message['is_multipart'] = False
        observable[str(object_num)] = dict(message)
        return observable

    def resolve_email_object_pattern(self, attributes, object_id):
        pattern_mapping = objectsMapping['email']['pattern']
        pattern = []
        n = 0
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            relation = attribute['object_relation']
            try:
                mapping = emailObjectMapping[relation]
                email_type = mapping['email_type']
                if relation in ('attachment', 'screenshot'):
                    stix_type = mapping['stix_type'].format(n)
                    if 'data' in attribute and attribute['data']:
                        pattern.append(pattern_mapping.format(email_type, 'body_multipart[{}].body_raw_ref.payload_bin'.format(n), attribute['data']))
                    n += 1
                else:
                    stix_type = mapping['stix_type']
            except KeyError:
                email_type = 'message'
                stix_type = "'x_misp_{}_{}'".format(attribute['type'], relation)
                if relation == 'eml':
                    stix_type_data = "{}.data".format(stix_type)
                    pattern.append(pattern_mapping.format(email_type, stix_type_data, attribute['data']))
                    stix_type += ".value"
            pattern.append(pattern_mapping.format(email_type, stix_type, attribute['value']))
        return pattern

    def resolve_file_observable(self, attributes, object_id):
        observable = {}
        file_observable = defaultdict(dict)
        file_observable['type'] = 'file'
        n_object = 0
        attributes_dict = self.create_file_attributes_dict(attributes, object_id)
        attributes_dict.update({'path': ['/home/chrisr3d/git/'], 'fullpath': ['/home/chrisr3d/git/MISP/cleanMISP/app/files/scripts/stix2']})
        for key, feature in fileMapping.items():
            if key in attributes_dict:
                if key in hash_types:
                    file_observable['hashes'][feature] = attributes_dict[key]
                else:
                    file_observable[feature] = attributes_dict[key]
        if 'filename' in attributes_dict:
            file_observable['name'] = attributes_dict['filename'][0]
            if len(attributes_dict['filename']) > 1:
                self._handle_multiple_file_fields_observable(file_observable, attributes_dict['filename'][1:], 'filename')
        if 'path' in attributes_dict:
            observable[str(n_object)] = {'type': 'directory', 'path': attributes_dict['path'][0]}
            file_observable['parent_directory_ref'] = str(n_object)
            n_object += 1
            if len(attributes_dict['path']) > 1:
                self._handle_multiple_file_fields_observable(file_obsevrable, attributes_dict['path'][1:], 'path')
        if 'fullpath' in attributes_dict:
            if 'parent_directory_ref' not in file_observable:
                observable[str(n_object)] = {'type': 'directory', 'path': attributes_dict['fullpath'][0]}
                file_observable['parent_directory_ref'] = str(n_object)
                n_object += 1
                if len(attributes_dict['path']) > 1:
                    self._handle_multiple_file_fields_observable(file_obsevrable, attributes_dict['fullpath'][1:], 'fullpath')
            else:
                self._handle_multiple_file_fields_observable(file_observable, attributes_dict['fullpath'], 'fullpath')
        if 'malware-sample' in attributes_dict:
            artifact, value = self._create_artifact_observable(attributes_dict['malware-sample'])
            filename, md5 = value.split('|')
            artifact['name'] = filename
            artifact['hashes'] = {'MD5': md5}
            observable[str(n_object)] = artifact
            file_observable['content_ref'] = str(n_object)
            n_object += 1
        if 'attachment' in attributes_dict:
            artifact, value = self._create_artifact_observable(attributes_dict['attachment'])
            artifact['name'] = value
            observable[str(n_object)] = artifact
            n_object += 1
        observable[str(n_object)] = file_observable
        return observable

    def resolve_file_pattern(self, attributes, object_id):
        patterns = []
        pattern = objectsMapping['file']['pattern']
        attributes_dict = self.create_file_attributes_dict(attributes, object_id)
        attributes_dict.update({'path': ['/home/chrisr3d/git/'], 'fullpath': ['/home/chrisr3d/git/MISP/cleanMISP/app/files/scripts/stix2']})
        for key, feature in fileMapping.items():
            if key in attributes_dict:
                if key in hash_types:
                    feature = f"hashes.'{feature}'"
                patterns.append(pattern.format(feature, attributes_dict[key]))
        if 'filename' in attributes_dict:
            self._handle_multiple_file_fields_pattern(patterns, attributes_dict['filename'], 'name')
        for feature in ('path', 'fullpath'):
            if feature in attributes_dict:
                self._handle_multiple_file_fields_pattern(patterns, attributes_dict[feature], 'parent_directory_ref.path')
        for feature, pattern_part in zip(('attachment', 'malware-sample'), ('artifact:', 'file:content_ref.')):
            if feature in attributes_dict:
                value = attributes_dict[feature]
                if ' | ' in value:
                    value, data = value.split(' | ')
                    patterns.append(f"{pattern_part}payload_bin = '{data}'")
                if feature == 'malware-sample':
                    value, md5 = value.split('|')
                    patterns.append(f"{pattern_part}hashes.'MD5' = '{md5}'")
                    patterns.append(f"{pattern_part}name = '{value}'")
                else:
                    patterns.append(f"{pattern_part}x_misp_text_name = '{value}'")
        return patterns

    def resolve_ip_port_observable(self, attributes, object_id):
        observable = {'type': 'network-traffic', 'protocols': ['tcp']}
        ip_address = {}
        domain = {}
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            relation = attribute['object_relation']
            attribute_value = attribute['value']
            if relation == 'ip':
                ip_address['type'] = define_address_type(attribute_value)
                ip_address['value'] = attribute_value
            elif relation == 'domain':
                domain['type'] = 'domain-name'
                domain['value'] = attribute_value
            else:
                try:
                    observable_type = ipPortObjectMapping[relation]
                except KeyError:
                    continue
                observable[observable_type] = attribute_value
        ref_type = 'dst_ref'
        main_observable = None
        if 'src_port' in observable or 'dst_port' in observable:
            for port in ('src_port', 'dst_port'):
                try:
                    port_value = defineProtocols[str(observable[port])]
                    if port_value not in observable['protocols']:
                        observable['protocols'].append(port_value)
                except KeyError:
                    pass
            main_observable = observable
        else:
            if domain:
                ref_type = 'resolves_to_refs'
        return self.ip_port_observable_to_return(ip_address, main_observable, domain, ref_type)

    @staticmethod
    def ip_port_observable_to_return(ip_address, d_object, domain, s_object):
        observable = {}
        o_id = 0
        if ip_address:
            observable['0'] = ip_address
            o_id += 1
        if d_object:
            if ip_address:
                d_object[s_object] = '0'
            observable[str(o_id)] = d_object
            o_id += 1
        if domain:
            if ip_address and not d_object:
                domain[s_object] = '0'
            observable[str(o_id)] = domain
        return observable

    def resolve_ip_port_pattern(self, attributes, object_id):
        pattern = []
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            relation = attribute['object_relation']
            attribute_value = attribute['value']
            if relation == 'domain':
                mapping_type = 'domain-ip'
                stix_type = ipPortObjectMapping[relation]
            elif relation == 'ip':
                mapping_type = 'ip-port'
                stix_type = ipPortObjectMapping[relation].format('ref', define_address_type(attribute_value))
            else:
                try:
                    stix_type = ipPortObjectMapping[relation]
                    mapping_type = 'ip-port'
                except KeyError:
                    continue
            pattern.append(objectsMapping[mapping_type]['pattern'].format(stix_type, attribute_value))
        return pattern

    def resolve_network_connection_observable(self, attributes, object_id):
        attributes = {attribute['object_relation']: attribute['value'] for attribute in attributes}
        n, network_object, observable = self.create_network_observable(attributes)
        protocols = [attributes[layer] for layer in ('layer3-protocol', 'layer4-protocol', 'layer7-protocol') if layer in attributes]
        network_object['protocols'] = protocols if protocols else ['tcp']
        observable[str(n)] = network_object
        return observable

    def resolve_network_connection_pattern(self, attributes, object_id):
        mapping = objectsMapping['network-connection']['pattern']
        attributes = {attribute['object_relation']: attribute['value'] for attribute in attributes}
        pattern = self.create_network_pattern(attributes, mapping)
        protocols = [attributes[layer] for layer in ('layer3-protocol', 'layer4-protocol', 'layer7-protocol') if layer in attributes]
        if protocols:
            for p in range(len(protocols)):
                pattern.append("network-traffic:protocols[{}] = '{}'".format(p, protocols[p]))
        return pattern

    def resolve_network_socket_observable(self, attributes, object_id):
        states, tmp_attributes = self.parse_network_socket_attributes(attributes, object_id)
        n, network_object, observable = self.create_network_observable(tmp_attributes)
        socket_extension = {networkTrafficMapping[feature]: tmp_attributes[feature] for feature in ('address-family', 'domain-family') if feature in tmp_attributes}
        for state in states:
            state_type = "is_{}".format(state)
            socket_extension[state_type] = True
        network_object['protocols'] = [tmp_attributes['protocol']] if 'protocol' in tmp_attributes else ['tcp']
        if socket_extension:
            network_object['extensions'] = {'socket-ext': socket_extension}
        observable[str(n)] = network_object
        return observable

    def resolve_network_socket_pattern(self, attributes, object_id):
        mapping = objectsMapping['network-socket']['pattern']
        states, tmp_attributes = self.parse_network_socket_attributes(attributes, object_id)
        pattern = self.create_network_pattern(tmp_attributes, mapping)
        stix_type = "extensions.'socket-ext'.{}"
        if "protocol" in tmp_attributes:
            pattern.append("network-traffic:protocols[0] = '{}'".format(tmp_attributes['protocol']))
        for feature in ('address-family', 'domain-family'):
            if feature in tmp_attributes:
                pattern.append(mapping.format(stix_type.format(networkTrafficMapping[feature]), tmp_attributes[feature]))
        for state in states:
            state_type = "is_{}".format(state)
            pattern.append(mapping.format(stix_type.format(state_type), True))
        return pattern

    def parse_network_socket_attributes(self, attributes, object_id):
        states = []
        tmp_attributes = {}
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            relation = attribute['object_relation']
            if relation == 'state':
                states.append(attribute['value'])
            else:
                tmp_attributes[relation] = attribute['value']
        return states, tmp_attributes

    def resolve_process_observable(self, attributes, object_id):
        observable = {}
        current_process = defaultdict(list)
        current_process['type'] = 'process'
        n = 0
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            relation = attribute['object_relation']
            if relation == 'parent-pid':
                str_n = str(n)
                observable[str_n] = {'type': 'process', 'pid': attribute['value']}
                current_process['parent_ref'] = str_n
                n += 1
            elif relation == 'child-pid':
                str_n = str(n)
                observable[str_n] = {'type': 'process', 'pid': attribute['value']}
                current_process['child_refs'].append(str_n)
                n += 1
            elif relation == 'image':
                str_n = str(n)
                observable[str_n] = {'type': 'file', 'name': attribute['value']}
                current_process['binary_ref'] = str_n
                n += 1
            else:
                try:
                    current_process[processMapping[relation]] = attribute['value']
                except KeyError:
                    pass
        observable[str(n)] = current_process
        return observable

    def resolve_process_pattern(self, attributes, object_id):
        mapping = objectsMapping['process']['pattern']
        pattern = []
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            try:
                pattern.append(mapping.format(processMapping[attribute['object_relation']], attribute['value']))
            except KeyError:
                continue
        return pattern

    def resolve_regkey_observable(self, attributes, object_id):
        observable = {'type': 'windows-registry-key'}
        values = {}
        registry_value_types = ('data', 'data-type', 'name')
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            relation = attribute['object_relation']
            try:
                stix_type = regkeyMapping[relation]
            except KeyError:
                stix_type = "x_misp_{}_{}".format(attribute['type'], relation)
            if relation in registry_value_types:
                values[stix_type] = attribute['value']
            else:
                observable[stix_type] = attribute['value']
        if values:
            observable['values'] = [values]
        return {'0': observable}

    def resolve_regkey_pattern(self, attributes, object_id):
        mapping = objectsMapping['registry-key']['pattern']
        pattern = []
        fields = ('key', 'value')
        registry_value_types = ('data', 'data-type', 'name')
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            relation = attribute['object_relation']
            try:
                stix_type = regkeyMapping[relation]
            except KeyError:
                stix_type = "'x_misp_{}_{}'".format(attribute['type'], relation)
            value = attribute['value'].strip().replace('\\', '\\\\') if relation in fields and '\\\\' not in attribute['value'] else attribute['value'].strip()
            if relation in registry_value_types:
                stix_type = "values.{}".format(stix_type)
            pattern.append(mapping.format(stix_type, value))
        return pattern

    @staticmethod
    def create_network_observable(attributes):
        n = 0
        network_object = {'type': 'network-traffic'}
        observable = {}
        for feature in ('src', 'dst'):
            ip_feature = 'ip-{}'.format(feature)
            host_feature = 'hostname-{}'.format(feature)
            refs = []
            if host_feature in attributes:
                str_n = str(n)
                observable[str_n] = {'type': 'domain-name', 'value': attributes[host_feature]}
                refs.append(str_n)
                n += 1
            if ip_feature in attributes:
                feature_value = attributes[ip_feature]
                str_n = str(n)
                observable[str_n] = {'type': define_address_type(feature_value), 'value': feature_value}
                refs.append(str_n)
                n +=1
            if refs:
                ref_str, ref_list = ('ref', refs[0]) if len(refs) == 1 else ('refs', refs)
                network_object['{}_{}'.format(feature, ref_str)] = ref_list
        for feature in ('src-port', 'dst-port'):
            if feature in attributes:
                network_object[networkTrafficMapping[feature]] = attributes[feature]
        return n, network_object, observable

    @staticmethod
    def create_network_pattern(attributes, mapping):
        pattern = []
        features = ('ip-{}', 'hostname-{}')
        for feature in ('src', 'dst'):
            index = 0
            references = {ftype: attributes[ftype] for ftype in (f_type.format(feature) for f_type in features) if ftype in attributes}
            ref  = 'ref' if len(references) == 1 else 'ref[{}]'
            if f'ip-{feature}' in attributes:
                value = references[f'ip-{feature}']
                pattern.append(mapping.format(networkTrafficMapping[f'ip-{feature}'].format(ref.format(index), define_address_type(value)), value))
                index += 1
            if f'hostname-{feature}' in attributes:
                key = f'hostname-{feature}'
                pattern.append(mapping.format(networkTrafficMapping[key].format(ref.format(index), 'domain-name'), references[key]))
            if f'{feature}-port' in attributes:
                key = f'{feature}-port'
                pattern.append(mapping.format(networkTrafficMapping[key], attributes[key]))
        return pattern

    @staticmethod
    def resolve_stix2_pattern(attributes, _):
        for attribute in attributes:
            if attribute['object_relation'] == 'stix2-pattern':
                return attribute['value']

    def resolve_url_observable(self, attributes, object_id):
        url_args = {}
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            if attribute['type'] == 'url':
                # If we have the url (WE SHOULD), we return the observable supported atm with the url value
                observable = {'0': {'type': 'url', 'value': attribute['value']}}
            else:
                # otherwise, we need to see if there is a port or domain value to parse
                url_args[attribute['type']] = attribute['value']
        if 'domain' in url_args:
            observable['1'] = {'type': 'domain-name', 'value': url_args['domain']}
        if 'port' in url_args:
            port_value = url_args['port']
            port = {'type': 'network-traffic', 'dst_ref': '1', 'protocols': ['tcp'], 'dst_port': port_value}
            try:
                port['protocols'].append(defineProtocols[port_value])
            except KeyError:
                pass
            if '1' in observable:
                observable['2'] = port
            else:
                observable['1'] = port
        return observable

    def resolve_url_pattern(self, attributes, object_id):
        pattern = []
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            attribute_type = attribute['type']
            try:
                stix_type = urlMapping[attribute_type]
            except KeyError:
                continue
            if attribute_type == 'port':
                mapping = 'ip-port'
            elif attribute_type == 'domain':
                mapping = 'domain-ip'
            else:
                mapping = attribute_type
            pattern.append(objectsMapping[mapping]['pattern'].format(stix_type, attribute['value']))
        return pattern

    def resolve_user_account_observable(self, attributes, object_id):
        attributes = self.parse_user_account_attributes(attributes, object_id)
        observable = {'type': 'user-account'}
        extension = {}
        for relation, value in attributes.items():
            try:
                observable[userAccountMapping[relation]] = value
            except KeyError:
                try:
                    extension[unixAccountExtensionMapping[relation]] = value
                except KeyError:
                    continue
        if extension:
            observable['extensions'] = {'unix-account-ext': extension}
        return {'0': observable}

    def resolve_user_account_pattern(self, attributes, object_id):
        mapping = objectsMapping['user-account']['pattern']
        extension_pattern = "extensions.'unix-account-ext'.{}"
        attributes = self.parse_user_account_attributes(attributes, object_id)
        pattern = []
        if 'group' in attributes:
            i = 0
            for group in attributes.pop('group'):
                pattern.append(mapping.format(extension_pattern.format('groups[{}]'.format(i)), group))
                i += 1
        for relation, value in attributes.items():
            try:
                pattern_part = mapping.format(userAccountMapping[relation], value)
            except KeyError:
                try:
                    pattern_part = mapping.format(extension_pattern.format(unixAccountExtensionMapping[relation]), value)
                except KeyError:
                    continue
            pattern.append(pattern_part)
        return pattern

    def parse_user_account_attributes(self, attributes, object_id):
        tmp_attributes = defaultdict(list)
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            relation = attribute['object_relation']
            if relation == 'group':
                tmp_attributes[relation].append(attribute['value'])
            else:
                tmp_attributes[relation] = attribute['value']
        if 'user-id' not in tmp_attributes and 'username' in tmp_attributes:
            tmp_attributes['user-id'] = tmp_attributes.pop('username')
        if 'text' in tmp_attributes:
            tmp_attributes.pop('text')
        return tmp_attributes

    def resolve_x509_observable(self, attributes, object_id):
        observable = {'type': 'x509-certificate'}
        hashes = {}
        attributes2parse = defaultdict(list)
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            relation = attribute['object_relation']
            if relation in ("x509-fingerprint-md5", "x509-fingerprint-sha1", "x509-fingerprint-sha256"):
                hashes[relation.split('-')[2]] = attribute['value']
            else:
                try:
                    observable[x509mapping[relation]] = attribute['value']
                except KeyError:
                    value = bool(attribute['value']) if attribute['type'] == 'boolean' else attribute['value']
                    attributes2parse["x_misp_{}_{}".format(attribute['type'], relation)].append(value)
        if hashes:
            observable['hashes'] = hashes
        for stix_type, value in attributes2parse.items():
            observable[stix_type] = value if len(value) > 1 else value[0]
        return {'0': observable}

    def resolve_x509_pattern(self, attributes, object_id):
        mapping = objectsMapping['x509']['pattern']
        pattern = []
        for attribute in attributes:
            self.parse_galaxies(attribute['Galaxy'], object_id)
            relation = attribute['object_relation']
            if relation in ("x509-fingerprint-md5", "x509-fingerprint-sha1", "x509-fingerprint-sha256"):
                stix_type = fileMapping['hashes'].format(relation.split('-')[2])
            else:
                try:
                    stix_type = x509mapping[relation]
                except KeyError:
                    stix_type = "'x_misp_{}_{}'".format(attribute['type'], relation)
            value = bool(attribute['value']) if attribute['type'] == 'boolean' else attribute['value']
            pattern.append(mapping.format(stix_type, value))
        return pattern

    @staticmethod
    def _create_artifact_observable(value):
        artifact = {'type': 'artifact'}
        if ' | ' in value:
            value, data = value.split(' | ')
            artifact['payload_bin'] = data
        return artifact, value

    def create_file_attributes_dict(self, attributes, object_id):
        multiple_fields = ('filename', 'path', 'fullpath')
        attributes_dict = defaultdict(list)
        for attribute in attributes:
            attributes_dict[attribute['object_relation']].append(self._parse_attribute(attribute))
            self.parse_galaxies(attribute['Galaxy'], object_id)
        return {key: value[0] if key not in multiple_fields and len(value) == 1 else value for key, value in attributes_dict.items()}

    @staticmethod
    def _handle_multiple_file_fields_observable(file_observable, values, feature):
        if len(values) > 1:
            file_observable[f'x_misp_multiple_{feature}s'] = values
        else:
            file_observable[f'x_misp_multiple_{feature}'] = values[0]

    @staticmethod
    def _handle_multiple_file_fields_pattern(patterns, values, feature):
        if len(values) > 1:
            patterns.extend([f"file:{feature} = '{value}'" for value in values])
        else:
            patterns.append(f"file:{feature} = '{values[0]}'")

    @staticmethod
    def _parse_attribute(attribute):
        if attribute['type'] in ('attachment', 'malware-sample') and attribute.get('data') is not None:
            return f"{attribute['value'].replace(' | ', '|')} | {attribute['data']}"
        return attribute['value']

    @staticmethod
    def define_attribute_value(value, comment):
        if value.isdigit() or value.startswith("AS"):
            return int(value) if value.isdigit() else int(value[2:].split(' ')[0])
        if comment.startswith("AS") or comment.isdigit():
            return int(comment) if comment.isdigit() else int(comment[2:].split(' ')[0])

    @staticmethod
    def get_datetime_from_timestamp(timestamp):
        return datetime.datetime.utcfromtimestamp(int(timestamp))

    @staticmethod
    def handle_time_fields(attribute, timestamp, stix_type):
        to_return = {'created': timestamp, 'modified': timestamp}
        for misp_field, stix_field in zip(('first_seen', 'last_seen'), _time_fields[stix_type]):
            to_return[stix_field] = attribute[misp_field] if attribute[misp_field] else timestamp
        return to_return

def main(args):
    stix_builder = StixBuilder()
    stix_builder.loadEvent(args)
    stix_builder.buildEvent()

if __name__ == "__main__":
    main(sys.argv)
