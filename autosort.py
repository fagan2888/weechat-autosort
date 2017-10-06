# -*- coding: utf-8 -*-
#
# Copyright (C) 2013-2017 Maarten de Vries <maarten@de-vri.es>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

#
# Autosort automatically keeps your buffers sorted and grouped by server.
# You can define your own sorting rules. See /help autosort for more details.
#
# http://github.com/de-vri-es/weechat-autosort
#

#
# Changelog:
# 3.0:
#   * Switch to evaluated expressions for sorting.
#   * Add ${info:replace,from,to,text} as helper pattern.
#   * Make tab completion context aware.
# 2.8:
#   * Fix compatibility with python 3 regarding unicode handling.
# 2.7:
#   * Fix sorting of buffers with spaces in their name.
# 2.6:
#   * Ignore case in rules when doing case insensitive sorting.
# 2.5:
#   * Fix handling unicode buffer names.
#   * Add hint to set irc.look.server_buffer to independent and buffers.look.indenting to on.
# 2.4:
#   * Make script python3 compatible.
# 2.3:
#   * Fix sorting items without score last (regressed in 2.2).
# 2.2:
#   * Add configuration option for signals that trigger a sort.
#   * Add command to manually trigger a sort (/autosort sort).
#   * Add replacement patterns to apply before sorting.
# 2.1:
#   * Fix some minor style issues.
# 2.0:
#   * Allow for custom sort rules.
#


import weechat
import re
import json
import time

SCRIPT_NAME     = 'autosort'
SCRIPT_AUTHOR   = 'Maarten de Vries <maarten@de-vri.es>'
SCRIPT_VERSION  = '3.0'
SCRIPT_LICENSE  = 'GPL3'
SCRIPT_DESC     = 'Flexible automatic (or manual) buffer grouping and sorting based on eval expressions.'


config = None
hooks  = []
timer  = None

if hasattr(time, 'perf_counter'):
	perf_counter = time.perf_counter
else:
	perf_counter = time.clock

def casefold(string):
	if hasattr(string, 'casefold'): return string.casefold()
	# Fall back to lowercasing for python2.
	return string.lower()

def list_swap(values, a, b):
	values[a], values[b] = values[b], values[a]

def list_move(values, old_index, new_index):
	values.insert(new_index, values.pop(old_index))

class HumanReadableError(Exception):
	pass

def parse_int(arg, arg_name = 'argument'):
	''' Parse an integer and provide a more human readable error. '''
	arg = arg.strip()
	try:
		return int(arg)
	except ValueError:
		raise HumanReadableError('Invalid {0}: expected integer, got "{1}".'.format(arg_name, arg))

def decode_rules(blob):
	parsed = json.loads(blob)
	if not isinstance(parsed, list):
		log('Malformed rules, expected a JSON encoded list of strings, but got a {0}. No rules have been loaded. Please fix the setting manually.'.format(type(parsed)))
		return []

	for i, entry in enumerate(parsed):
		if not isinstance(entry, (str, unicode)):
			log('Rule #{0} is not a string but a {1}. No rules have been loaded. Please fix the setting manually.'.format(i, type(entry)))
			return []

	return parsed

def decode_helpers(blob):
	parsed = json.loads(blob)
	if not isinstance(parsed, dict):
		log('Malformed helpers, expected a JSON encoded dictonary but got a {0}. No helpers have been loaded. Please fix the setting manually.'.format(type(parsed)))
		return {}

	for key, value in parsed.items():
		if not isinstance(value, (str, unicode)):
			log('Helper "{0}" is not a string but a {1}. No helpers have been loaded. Please fix seting manually.'.format(key, type(value)))
			return {}
	return parsed

class Config:
	''' The autosort configuration. '''

	default_rules = json.dumps([
		'${core_first}',
		'${irc_last}',
		'${buffer.plugin.name}',
		'${server}',
		'${servers_first}',
		'${channels_first}',
		'${hashless_name}',
	])

	default_helpers = json.dumps({
		'core_first':     '${if:${buffer.full_name}==core.weechat?0:1}',
		'irc_first':      '${if:${buffer.plugin.name}==irc?0:1}',
		'irc_last':       '${if:${buffer.plugin.name}==irc?1:0}',
		'servers_first':  '${if:${type}==server?0:1}',
		'channels_first': '${if:${type}==channel?0:1}',
		'private_first':  '${if:${type}==private?0:1}',
		'hashless_name':  '${info:autosort_replace,#,,${buffer.name}}',
	})

	default_signals = 'buffer_opened buffer_merged buffer_unmerged buffer_renamed'

	def __init__(self, filename):
		''' Initialize the configuration. '''

		self.filename         = filename
		self.config_file      = weechat.config_new(self.filename, '', '')
		self.sorting_section  = None
		self.v3_section       = None

		self.case_sensitive   = False
		self.rules            = []
		self.helpers          = {}
		self.signals          = []
		self.signal_delay     = 100
		self.sort_on_config   = True

		self.__case_sensitive = None
		self.__rules          = None
		self.__helpers        = None
		self.__signals        = None
		self.__signal_delay   = None
		self.__sort_on_config = None

		if not self.config_file:
			log('Failed to initialize configuration file "{0}".'.format(self.filename))
			return

		self.sorting_section = weechat.config_new_section(self.config_file, 'sorting', False, False, '', '', '', '', '', '', '', '', '', '')
		self.v3_section      = weechat.config_new_section(self.config_file, 'v3',      False, False, '', '', '', '', '', '', '', '', '', '')

		if not self.sorting_section:
			log('Failed to initialize section "sorting" of configuration file.')
			weechat.config_free(self.config_file)
			return

		self.__case_sensitive = weechat.config_new_option(
			self.config_file, self.sorting_section,
			'case_sensitive', 'boolean',
			'If this option is on, sorting is case sensitive.',
			'', 0, 0, 'off', 'off', 0,
			'', '', '', '', '', ''
		)

		self.__rules = weechat.config_new_option(
			self.config_file, self.v3_section,
			'rules', 'string',
			'An ordered list of sorting rules encoded as JSON. See /help autosort for commands to manipulate these rules.',
			'', 0, 0, Config.default_rules, Config.default_rules, 0,
			'', '', '', '', '', ''
		)

		self.__helpers = weechat.config_new_option(
			self.config_file, self.v3_section,
			'helpers', 'string',
			'A dictionary helper variables to use in the sorting rules, encoded as JSON. See /help autosort for commands to manipulate these helpers.',
			'', 0, 0, Config.default_helpers, Config.default_helpers, 0,
			'', '', '', '', '', ''
		)

		self.__signals = weechat.config_new_option(
			self.config_file, self.sorting_section,
			'signals', 'string',
			'A space separated list of signals that will cause autosort to resort your buffer list.',
			'', 0, 0, Config.default_signals, Config.default_signals, 0,
			'', '', '', '', '', ''
		)

		self.__signal_delay = weechat.config_new_option(
			self.config_file, self.sorting_section,
			'signal_delay', 'integer',
			'Delay in milliseconds to wait after a signal before sorting the buffer list. This prevents triggering many times if multiple signals arrive in a short time. It can also be needed to wait for buffer localvars to be available.',
			'', 0, 1000, "5", "100", 0,
			'', '', '', '', '', ''
		)

		self.__sort_on_config = weechat.config_new_option(
			self.config_file, self.sorting_section,
			'sort_on_config_change', 'boolean',
			'Decides if the buffer list should be sorted when autosort configuration changes.',
			'', 0, 0, 'on', 'on', 0,
			'', '', '', '', '', ''
		)

		if weechat.config_read(self.config_file) != weechat.WEECHAT_RC_OK:
			log('Failed to load configuration file.')

		if weechat.config_write(self.config_file) != weechat.WEECHAT_RC_OK:
			log('Failed to write configuration file.')

		self.reload()

	def reload(self):
		''' Load configuration variables. '''

		self.case_sensitive = weechat.config_boolean(self.__case_sensitive)

		rules_blob    = weechat.config_string(self.__rules)
		helpers_blob  = weechat.config_string(self.__helpers)
		signals_blob  = weechat.config_string(self.__signals)

		self.rules          = decode_rules(rules_blob)
		self.helpers        = decode_helpers(helpers_blob)
		self.signals        = signals_blob.split()
		self.signal_delay   = weechat.config_integer(self.__signal_delay)
		self.sort_on_config = weechat.config_boolean(self.__sort_on_config)

	def save_rules(self, run_callback = True):
		''' Save the current rules to the configuration. '''
		weechat.config_option_set(self.__rules, json.dumps(self.rules), run_callback)

	def save_helpers(self, run_callback = True):
		''' Save the current helpers to the configuration. '''
		weechat.config_option_set(self.__helpers, json.dumps(self.helpers), run_callback)


def pad(sequence, length, padding = None):
	''' Pad a list until is has a certain length. '''
	return sequence + [padding] * max(0, (length - len(sequence)))


def log(message, buffer = 'NULL'):
	weechat.prnt(buffer, 'autosort: {0}'.format(message))


def get_buffers():
	''' Get a list of all the buffers in weechat. '''
	hdata  = weechat.hdata_get('buffer')
	buffer = weechat.hdata_get_list(hdata, "gui_buffers");

	result = []
	while buffer:
		number = weechat.hdata_integer(hdata, buffer, 'number')
		result.append((number, buffer))
		buffer = weechat.hdata_pointer(hdata, buffer, 'next_buffer')
	return hdata, result

class MergedBuffers(list):
	""" A list of merged buffers, possibly of size 1. """
	def __init__(self, number):
		super(MergedBuffers, self).__init__()
		self.number = number

def merge_buffer_list(buffers):
	'''
	Group merged buffers together.
	The output is a list of MergedBuffers.
	'''
	if not buffers: return []
	result = {}
	for number, buffer in buffers:
		if number not in result: result[number] = MergedBuffers(number)
		result[number].append(buffer)
	return result.values()

def sort_buffers(hdata, buffers, rules, helpers, case_sensitive):
	for merged in buffers:
		for buffer in merged:
			name = weechat.hdata_string(hdata, buffer, 'name')

	return sorted(buffers, key=merged_sort_key(rules, helpers, case_sensitive))

def buffer_sort_key(rules, helpers, case_sensitive):
	''' Create a sort key function for a list of lists of merged buffers. '''
	def key(buffer):
		extra_vars = {}
		for helper_name, helper in sorted(helpers.items()):
			expanded = weechat.string_eval_expression(helper, {"buffer": buffer}, {}, {})
			extra_vars[helper_name] = expanded if case_sensitive else casefold(expanded)
		result = []
		for rule in rules:
			expanded = weechat.string_eval_expression(rule, {"buffer": buffer}, extra_vars, {})
			result.append(expanded if case_sensitive else casefold(expanded))
		return result

	return key

def merged_sort_key(rules, helpers, case_sensitive):
	buffer_key = buffer_sort_key(rules, helpers, case_sensitive)
	def key(merged):
		best = None
		for buffer in merged:
			this = buffer_key(buffer)
			if best is None or this < best: best = this
		return best
	return key

def apply_buffer_order(buffers):
	''' Sort the buffers in weechat according to the given order. '''
	for i, buffer in enumerate(buffers):
		weechat.buffer_set(buffer[0], "number", str(i + 1))

def split_args(args, expected, optional = 0):
	''' Split an argument string in the desired number of arguments. '''
	split = args.split(' ', expected - 1)
	if (len(split) < expected):
		raise HumanReadableError('Expected at least {0} arguments, got {1}.'.format(expected, len(split)))
	return split[:-1] + pad(split[-1].split(' ', optional), optional + 1, '')

def do_sort():
	hdata, buffers = get_buffers()
	buffers = merge_buffer_list(buffers)
	buffers = sort_buffers(hdata, buffers, config.rules, config.helpers, config.case_sensitive)
	apply_buffer_order(buffers)

def command_sort(buffer, command, args):
	''' Sort the buffers and print a confirmation. '''
	do_sort()
	log("Finished sorting buffers.", buffer)
	return weechat.WEECHAT_RC_OK

def command_debug(buffer, command, args):
	hdata, buffers = get_buffers()
	buffers = merge_buffer_list(buffers)

	# Show evaluation results.
	log('Individual evaluation results:')
	start = perf_counter()
	key = buffer_sort_key(config.rules, config.helpers, config.case_sensitive)
	results = []
	for merged in buffers:
		for buffer in merged:
			fullname = weechat.hdata_string(hdata, buffer, 'full_name')
			if isinstance(fullname, bytes): fullname = fullname.decode('utf-8')
			results.append((fullname, key(buffer)))
	elapsed = perf_counter() - start

	for fullname, result in results:
			log('{0}: {1}'.format(fullname, result))
	log('Computing evalutaion results took {0:.4f} seconds.'.format(elapsed))

	return weechat.WEECHAT_RC_OK

def command_rule_list(buffer, command, args):
	''' Show the list of sorting rules. '''
	output = 'Sorting rules:\n'
	for i, rule in enumerate(config.rules):
		output += '    {0}: {1}\n'.format(i, rule)
	if not len(config.rules):
		output += '    No sorting rules configured.\n'
	log(output, buffer)

	return weechat.WEECHAT_RC_OK


def command_rule_add(buffer, command, args):
	''' Add a rule to the rule list. '''
	config.rules.append(args)
	config.save_rules()
	command_rule_list(buffer, command, '')

	return weechat.WEECHAT_RC_OK


def command_rule_insert(buffer, command, args):
	''' Insert a rule at the desired position in the rule list. '''
	index, rule = split_args(args, 2)
	index = parse_int(index, 'index')

	config.rules.insert(index, rule)
	config.save_rules()
	command_rule_list(buffer, command, '')
	return weechat.WEECHAT_RC_OK


def command_rule_update(buffer, command, args):
	''' Update a rule in the rule list. '''
	index, rule = split_args(args, 2)
	index = parse_int(index, 'index')

	config.rules[index] = rule
	config.save_rules()
	command_rule_list(buffer, command, '')
	return weechat.WEECHAT_RC_OK


def command_rule_delete(buffer, command, args):
	''' Delete a rule from the rule list. '''
	index = args.strip()
	index = parse_int(index, 'index')

	config.rules.pop(index)
	config.save_rules()
	command_rule_list(buffer, command, '')
	return weechat.WEECHAT_RC_OK


def command_rule_move(buffer, command, args):
	''' Move a rule to a new position. '''
	index_a, index_b = split_args(args, 2)
	index_a = parse_int(index_a, 'index')
	index_b = parse_int(index_b, 'index')

	list_move(config.rules, index_a, index_b)
	config.save_rules()
	command_rule_list(buffer, command, '')
	return weechat.WEECHAT_RC_OK


def command_rule_swap(buffer, command, args):
	''' Swap two rules. '''
	index_a, index_b = split_args(args, 2)
	index_a = parse_int(index_a, 'index')
	index_b = parse_int(index_b, 'index')

	list_swap(config.rules, index_a, index_b)
	config.save_rules()
	command_rule_list(buffer, command, '')
	return weechat.WEECHAT_RC_OK


def command_helper_list(buffer, command, args):
	''' Show the list of helpers. '''
	output = 'Helper variables:\n'

	width = max(map(lambda x: len(x) if len(x) <= 30 else 0, config.helpers.keys()))

	for name, expression in sorted(config.helpers.items()):
		output += '    {0:>{width}}: {1}\n'.format(name, expression, width=width)
	if not len(config.helpers):
		output += '    No helper variables configured.'
	log(output)

	return weechat.WEECHAT_RC_OK


def command_helper_set(buffer, command, args):
	''' Add/update a helper to the helper list. '''
	name, expression = split_args(args, 2)

	config.helpers[name] = expression
	config.save_helpers()
	command_helper_list(buffer, command, '')

	return weechat.WEECHAT_RC_OK

def command_helper_delete(buffer, command, args):
	''' Delete a helper from the helper list. '''
	name = args.strip()

	del config.helpers[name]
	config.save_helpers()
	command_helper_list(buffer, command, '')
	return weechat.WEECHAT_RC_OK


def command_helper_rename(buffer, command, args):
	''' Rename a helper to a new position. '''
	old_name, new_name = split_args(args, 2)

	try:
		config.helpers[new_name] = config.helpers[old_name]
		del config.helpers[old_name]
	except KeyError:
		raise HumanReadableError('No such helper: {0}'.format(old_name))
	config.save_helpers()
	command_helper_list(buffer, command, '')
	return weechat.WEECHAT_RC_OK


def command_helper_swap(buffer, command, args):
	''' Swap two helpers. '''
	a, b = split_args(args, 2)
	try:
		config.helpers[b], config.helpers[a] = config.helpers[a], config.helpers[b]
	except KeyError as e:
		raise HumanReadableError('No such helper: {0}'.format(e.args[0]))

	config.helpers.swap(index_a, index_b)
	config.save_helpers()
	command_helper_list(buffer, command, '')
	return weechat.WEECHAT_RC_OK

def call_command(buffer, command, args, subcommands):
	''' Call a subccommand from a dictionary. '''
	subcommand, tail = pad(args.split(' ', 1), 2, '')
	subcommand = subcommand.strip()
	if (subcommand == ''):
		child   = subcommands.get(' ')
	else:
		command = command + [subcommand]
		child   = subcommands.get(subcommand)

	if isinstance(child, dict):
		return call_command(buffer, command, tail, child)
	elif callable(child):
		return child(buffer, command, tail)

	log('{0}: command not found'.format(' '.join(command)))
	return weechat.WEECHAT_RC_ERROR

def on_signal(*args, **kwargs):
	global timer
	''' Called whenever the buffer list changes. '''
	if timer is not None:
		weechat.unhook(timer)
		timer = None
	weechat.hook_timer(config.signal_delay, 0, 1, "on_timeout", "")
	return weechat.WEECHAT_RC_OK

def on_timeout(pointer, remaining_calls):
	global timer
	timer = None
	do_sort()
	return weechat.WEECHAT_RC_OK

def on_config_changed(*args, **kwargs):
	''' Called whenever the configuration changes. '''
	config.reload()

	# Unhook all signals and hook the new ones.
	for hook in hooks:
		weechat.unhook(hook)
	for signal in config.signals:
		hooks.append(weechat.hook_signal(signal, 'on_signal', ''))

	if config.sort_on_config:
		do_sort()

	return weechat.WEECHAT_RC_OK

def parse_arg(args):
	if not args: return None, None

	result  = ''
	escaped = False
	for i, c in enumerate(args):
		if not escaped:
			if c == '\\':
				escaped = True
				continue
			elif c == ',':
				return result, args[i+1:]
		result  += c
		escaped  = False
	return result, None

def parse_args(args, count):
	result = []
	for i in range(count):
		arg, args = parse_arg(args)
		if arg is None: break
		result.append(arg)
	return result, args

def on_info_replace(pointer, name, arguments):
	arguments, rest = parse_args(arguments, 3)
	if rest or len(arguments) < 3:
		log('usage: ${{info:{0},old,new,text}}'.format(name))
		return ''
	old, new, text = arguments

	return text.replace(old, new)


def on_autosort_command(data, buffer, args):
	''' Called when the autosort command is invoked. '''
	try:
		return call_command(buffer, ['/autosort'], args, {
			' ':      command_sort,
			'sort':   command_sort,
			'debug':  command_debug,

			'rules': {
				' ':         command_rule_list,
				'list':      command_rule_list,
				'add':       command_rule_add,
				'insert':    command_rule_insert,
				'update':    command_rule_update,
				'delete':    command_rule_delete,
				'move':      command_rule_move,
				'swap':      command_rule_swap,
			},
			'helpers': {
				' ':      command_helper_list,
				'list':   command_helper_list,
				'set':    command_helper_set,
				'delete': command_helper_delete,
				'rename': command_helper_rename,
				'swap':   command_helper_swap,
			},
		})
	except HumanReadableError as e:
		log(e, buffer)
		return weechat.WEECHAT_RC_ERROR

def add_completions(completion, words):
	for word in words:
		weechat.hook_completion_list_add(completion, word, 0, weechat.WEECHAT_LIST_POS_END)

def autosort_complete_rules(words, completion):
	if len(words) == 0:
		add_completions(completion, ['add', 'delete', 'insert', 'list', 'move', 'swap', 'update'])
	if len(words) == 1 and words[0] in ('delete', 'insert', 'move', 'swap', 'update'):
		add_completions(completion, map(str, range(len(config.rules))))
	if len(words) == 2 and words[0] in ('move', 'swap'):
		add_completions(completion, map(str, range(len(config.rules))))
	if len(words) == 2 and words[0] in ('update'):
		try:
			add_completions(completion, [config.rules[int(words[1])]])
		except KeyError: pass
		except ValueError: pass
	else:
		add_completions(completion, [''])
	return weechat.WEECHAT_RC_OK

def autosort_complete_helpers(words, completion):
	if len(words) == 0:
		add_completions(completion, ['delete', 'list', 'rename', 'set', 'swap'])
	elif len(words) == 1 and words[0] in ('delete', 'rename', 'set', 'swap'):
		add_completions(completion, sorted(config.helpers.keys()))
	elif len(words) == 2 and words[0] == 'swap':
		add_completions(completion, sorted(config.helpers.keys()))
	elif len(words) == 2 and words[0] == 'rename':
		add_completions(completion, sorted(config.helpers.keys()))
	elif len(words) == 2 and words[0] == 'set':
		try:
			add_completions(completion, [config.helpers[words[1]]])
		except KeyError: pass
	return weechat.WEECHAT_RC_OK

def on_autosort_complete(data, name, buffer, completion):
	cmdline = weechat.buffer_get_string(buffer, "input")
	cursor  = weechat.buffer_get_integer(buffer, "input_pos")
	prefix  = cmdline[:cursor]
	words   = prefix.split()[1:]

	# If the current word isn't finished yet,
	# ignore it for coming up with completion suggestions.
	if prefix[-1] != ' ': words = words[:-1]

	if len(words) == 0:
		add_completions(completion, ['debug', 'helpers', 'rules', 'sort'])
	elif words[0] == 'rules':
		return autosort_complete_rules(words[1:], completion)
	elif words[0] == 'helpers':
		return autosort_complete_helpers(words[1:], completion)
	return weechat.WEECHAT_RC_OK

command_description = r'''
NOTE: For the best effect, you may want to consider setting the option irc.look.server_buffer to independent and buffers.look.indenting to on.

# Commands

## Miscellaneous
/autosort sort
Manually trigger the buffer sorting.

/autosort debug
Show the evaluation results of the sort rules for each buffer.


## Sorting rules

/autosort rules list
Print the list of sort rules.

/autosort rules add <expression>
Add a new rule at the end of the list.

/autosort rules insert <index> <expression>
Insert a new rule at the given index in the list.

/autosort rules update <index> <expression>
Update a rule in the list with a new expression.

/autosort rules delete <index>
Delete a rule from the list.

/autosort rules move <index_from> <index_to>
Move a rule from one position in the list to another.

/autosort rules swap <index_a> <index_b>
Swap two rules in the list


## Helper variables

/autosort helpers list
Print the list of helper variables.

/autosort helpers set <name> <expression>
Add or update a helper variable with the given name.

/autosort helpers delete <named>
Delete a helper variable.

/autosort helpers rename <old_name> <new_name>
Rename a helper variable.

/autosort helpers swap <name_a> <name_b>
Swap the expressions of two helper variables in the list.


# Introduction
Autosort is a weechat script to automatically keep your buffers sorted.
The sort order can be customized by defining your own sort rules,
but the default should be sane enough for most people.
It can also group IRC channel/private buffers under their server buffer if you like.

## Sort rules
Autosort evaluates a list of eval expressions (see /help eval) and sorts the buffers based on evaluated result.
Earlier rules will be considered first.
Only if earlier rules produced identical results is the result of the next rule considered for sorting purposes.

You can debug your sort rules with the `/autosort debug` command, which will print the evaluation results of each rule for each buffer.

NOTE: The sort rules for version 3 are not compatible with version 2 or vice versa.
You will have to manually port your old rules to version 3 if you have any.

## Helper variables
You may define helper variables for the main sort rules to keep your rules readable.
They can be used in the main sort rules as variables.
For example, a helper variable named `foo` can be accessed in a main rule with the string `${foo}`.

## Replacing substrings
There is no default method for replacing text inside eval expressions.
However, autosort adds a `replace` info hook that can be used inside eval expressions: `${info:autosort_replace,from,to,text}`.
For example, `${info:autosort_replace,#,,${buffer.name}}` will evaluate to the buffer name with all hash signs stripped.
You can escape commas and backslashes inside the arguments by prefixing them with a backslash.

## Automatic or manual sorting
By default, autosort will automatically sort your buffer list whenever a buffer is opened, merged, unmerged or renamed.
This should keep your buffers sorted in almost all situations.
However, you may wish to change the list of signals that cause your buffer list to be sorted.
Simply edit the "autosort.sorting.signals" option to add or remove any signal you like.
If you remove all signals you can still sort your buffers manually with the "/autosort sort" command.
To prevent all automatic sorting, "autosort.sorting.sort_on_config_change" should also be set to off.
'''

command_completion = '%(plugin_autosort) %(plugin_autosort) %(plugin_autosort) %(plugin_autosort) %(plugin_autosort)'

info_replace_description = 'Replace all occurences of `from` with `to` in the string `text`.'
info_replace_arguments   = 'from,to,text'


if weechat.register(SCRIPT_NAME, SCRIPT_AUTHOR, SCRIPT_VERSION, SCRIPT_LICENSE, SCRIPT_DESC, "", ""):
	config = Config('autosort')

	weechat.hook_config('autosort.*', 'on_config_changed',  '')
	weechat.hook_completion('plugin_autosort', '', 'on_autosort_complete', '')
	weechat.hook_command('autosort', command_description, '', '', command_completion, 'on_autosort_command', 'NULL')
	weechat.hook_info('autosort_replace', info_replace_description, info_replace_arguments, 'on_info_replace', 'NULL')

	if config.sort_on_config:
		do_sort()
