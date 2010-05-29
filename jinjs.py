import os, json

from StringIO import StringIO

import jinja2
from jinja2 import nodes, loaders
from jinja2.runtime import Context, Undefined
from jinja2.visitor import NodeVisitor
from jinja2.nodes import EvalContext
from jinja2.compiler import Frame, UndeclaredNameVisitor, CompilerExit, VisitorExit, CodeGenerator
from jinja2.exceptions import TemplateAssertionError
from jinja2.utils import Markup, concat, escape, is_python_keyword, next

import PyV8 as pyv8

def find_undeclared(nodes, names):
	"""Check if the names passed are accessed undeclared.  The return value
	is a set of all the undeclared names from the sequence of names found.
	"""
	visitor = UndeclaredNameVisitor(names)
	try:
	    for node in nodes:
	        visitor.visit(node)
	except VisitorExit:
	    pass
	return visitor.undeclared

class CodeGenerator(CodeGenerator):
	def __init__(self, environment, name, filename, stream=None):
		if stream is None:
			stream = StringIO()
		self.environment = environment
		self.name = name
		self.filename = filename
		self.stream = stream

		self.blocks = {}

		self.has_known_extends = False

		self._last_identifier = 0
		self.filters = {}
		self.extends_so_far = 0

	def fail(self, x):
		raise TemplateAssertionError(msg, lineno, self.name, self.filename)

	def buffer(self, frame):
		"""Enable buffering for the frame from that point onwards."""
		frame.buffer = self.temporary_identifier()
		self.writeline('%s = [];' % frame.buffer)

	def return_buffer_contents(self, frame):
		"""Return the buffer contents of the frame."""
		if self.environment.autoescape:
			self.writeline('return Markup(concat(%s));' % frame.buffer)
		else:
			self.writeline('return %s.join();' % frame.buffer)

	def write(self, x):
		self.stream.write(x)

	def writeline(self, x):
		self.stream.write(x + '\n')

	def blockvisit(self, nodes, frame=None):
		for node in nodes:
			self.visit(node, frame)

	def pull_locals(self, frame):
		for name in frame.identifiers.undeclared:
			self.writeline('var l_%s = context.resolve(%s);' % (name, json.dumps(name)))

	def temporary_identifier(self):
		"""Get a new unique identifier."""
		self._last_identifier += 1
		return 't_%d' % self._last_identifier

	def push_scope(self, frame, extra_vars=()):
		"""This function returns all the shadowed variables in a dict
		in the form name: alias and will write the required assignments
		into the current scope.  No indentation takes place.

		This also predefines locally declared variables from the loop
		body because under some circumstances it may be the case that

		`extra_vars` is passed to `Frame.find_shadowed`.
		"""
		aliases = {}
		for name in frame.find_shadowed(extra_vars):
			aliases[name] = ident = self.temporary_identifier()
			self.writeline('%s = l_%s;' % (ident, name))
		to_declare = set()
		for name in frame.identifiers.declared_locally:
			if name not in aliases:
				to_declare.add('l_' + name)
		if to_declare:
			self.writeline(' = '.join(to_declare) + ' = missing;')
		return aliases

	def pop_scope(self, aliases, frame):
		"""Restore all aliases and delete unused variables."""
		for name, alias in aliases.iteritems():
			self.writeline('l_%s = %s;' % (name, alias))
		to_delete = set()
		for name in frame.identifiers.declared_locally:
			if name not in aliases:
				to_delete.add('l_' + name)
		if to_delete:
			# we cannot use the del statement here because enclosed
			# scopes can trigger a SyntaxError:
			#   a = 42; b = lambda: a; del a
			self.writeline(' = '.join(to_delete) + ' = missing;')

	def macro_body(self, node, frame, children=None):
		"""Dump the function def of a macro or call block."""
		frame = self.function_scoping(node, frame, children)
		# macros are delayed, they never require output checks
		frame.require_output_check = False
		args = frame.arguments
		# XXX: this is an ugly fix for the loop nesting bug
		# (tests.test_old_bugs.test_loop_call_bug).  This works around
		# a identifier nesting problem we have in general.  It's just more
		# likely to happen in loops which is why we work around it.  The
		# real solution would be "nonlocal" all the identifiers that are
		# leaking into a new python frame and might be used both unassigned
		# and assigned.
		if 'loop' in frame.identifiers.declared:
			args = args + ['l_loop=l_loop']
		self.writeline('function(%s) {' % ', '.join(args))
		self.buffer(frame)
		self.pull_locals(frame)
		self.blockvisit(node.body, frame)
		self.return_buffer_contents(frame)
		self.writeline('};')
		return frame

	def macro_def(self, node, frame):
		"""Dump the macro definition for the def created by macro_body."""
		arg_tuple = [x.name for x in node.args]
		name = getattr(node, 'name', None)
		self.write('new Jinjs.Macro(env, l_%s, %r, %s, [' %
				   (name, name, json.dumps(arg_tuple)))
		for arg in node.defaults:
			self.visit(arg, frame)
			self.write(', ')
		self.writeline('], %s, %s, %s);' % (
			json.dumps(bool(frame.accesses_kwargs)),
			json.dumps(bool(frame.accesses_varargs)),
			json.dumps(bool(frame.accesses_caller))
		))

	def visit_Template(self, node, frame=None):
		eval_ctx = EvalContext(self.environment, self.name)
		# do we have an extends tag at all?  If not, we can save some
		# overhead by just not processing any inheritance code.
		have_extends = node.find(nodes.Extends) is not None

		# find all blocks
		for block in node.find_all(nodes.Block):
			if block.name in self.blocks:
				self.fail('block %r defined twice' % block.name, block.lineno)
			self.blocks[block.name] = block

		self.writeline('(function(){return {')
		self.writeline('renderFunc: function(env, context, stream) {')
		frame = Frame(eval_ctx)
		frame.inspect(node.body)
		frame.toplevel = frame.rootlevel = True
		frame.require_output_check = have_extends and not self.has_known_extends
		if have_extends:
			self.writeline('var parentTemplate = undefined;')
		if 'self' in find_undeclared(node.body, ('self',)):
			frame.identifiers.add_special('self')
			self.writeline('var l_self = new Jinjs.TemplateReference(context);')

		self.pull_locals(frame)
		self.blockvisit(node.body, frame)

		# make sure that the parent root is called.
		if have_extends:
			if not self.has_known_extends:
				self.writeline('if (parentTemplate !== undefined) {')
			self.writeline('parentTemplate.renderFunc(env, context, stream);')
			if not self.has_known_extends:
				self.writeline('}')

		self.writeline('},')
		self.writeline('name: %s,' % json.dumps(self.name))
		self.writeline('blocks: {')

		# at this point we now have the blocks collected and can visit them too.
		for name, block in self.blocks.iteritems():
			block_frame = Frame()
			block_frame.inspect(block.body)
			block_frame.block = name
			self.writeline('%s: function(env, context, stream) {'
				           % name)
			undeclared = find_undeclared(block.body, ('self', 'super'))
			if 'self' in undeclared:
				block_frame.identifiers.add_special('self')
				self.writeline('var l_self = new Jinjs.TemplateReference(context);')
			if 'super' in undeclared:
				block_frame.identifiers.add_special('super')
				self.writeline('var l_super = context.super(%r, '
				               'block_%s)' % (name, name))
			self.pull_locals(block_frame)
			self.blockvisit(block.body, block_frame)
			self.writeline('},')

		self.writeline('}};})()')

	def visit_Block(self, node, frame):
		"""Call a block and register it for the template."""
		level = 0
		if frame.toplevel:
			# if we know that we are a child template, there is no need to
			# check if we are one
			if self.has_known_extends:
				return
			if self.extends_so_far > 0:
				self.writeline('if (parentTemplate === undefined) {')
				level += 1
		context = node.scoped and 'context.derived(locals())' or 'context'
		self.writeline('context.blocks[%r][0](env, %s, stream);' % (
				       node.name, context))
		self.writeline('}' * level)

	def visit_Extends(self, node, frame):
		"""Calls the extender."""
		if not frame.toplevel:
			self.fail('cannot use extend from a non top-level scope',
			          node.lineno)

		# if the number of extends statements in general is zero so
		# far, we don't have to add a check if something extended
		# the template before this one.
		if self.extends_so_far > 0:

			# if we have a known extends we just add a template runtime
			# error into the generated code.  We could catch that at compile
			# time too, but i welcome it not to confuse users by throwing the
			# same error at different times just "because we can".
			if not self.has_known_extends:
			    self.writeline('if (parentTemplate !== undefined) {')
			self.writeline('throw new TemplateRuntimeError(%r);' %
			               'extended multiple times')
			if not self.has_known_extends:
				self.writeline('}')

			# if we have a known extends already we don't need that code here
			# as we know that the template execution will end here.
			if self.has_known_extends:
			    raise CompilerExit()

		self.write('parentTemplate = env.get_template(')
		self.visit(node.template, frame)
		self.writeline(', %s);' % json.dumps(self.name))
		self.writeline('for (var name in parentTemplate.blocks) {')
		self.writeline('	if (parentTemplate.blocks.hasOwnProperty(name)) {')
		self.writeline('		if (!(name in context.blocks)) {')
		self.writeline('			context.blocks[name] = [];')
		self.writeline('		}')
		self.writeline('		context.blocks[name].push(parentTemplate.blocks[name]);')
		self.writeline('	}')
		self.writeline('}')

		# if this extends statement was in the root level we can take
		# advantage of that information and simplify the generated code
		# in the top level from this point onwards
		if frame.rootlevel:
			self.has_known_extends = True

		# and now we have one more
		self.extends_so_far += 1

	def visit_Include(self, node, frame):
		"""Handles includes."""
		if node.with_context:
			self.unoptimize_scope(frame)
		if node.ignore_missing:
			self.writeline('try {')

		func_name = 'get_or_select_template'
		if isinstance(node.template, nodes.Const):
			if isinstance(node.template.value, basestring):
				func_name = 'get_template'
			elif isinstance(node.template.value, (tuple, list)):
				func_name = 'select_template'
		elif isinstance(node.template, (nodes.Tuple, nodes.List)):
			func_name = 'select_template'

		self.writeline('template = env.%s(' % func_name)
		self.visit(node.template, frame)
		self.write(', %r)' % self.name)

		if node.with_context:
			self.writeline('for event in template.root_render_func('
						   'template.new_context(context.parent, True, '
						   'locals())):')
		else:
			self.writeline('for event in template.module._body_stream:')

		if node.ignore_missing:
			self.writeline('} catch(e) {}')

	def visit_Import(self, node, frame):
		"""Visit regular imports."""
		if node.with_context:
			self.unoptimize_scope(frame)
		self.write('var l_%s = ' % node.target)
		if frame.toplevel:
			self.write('context.vars[%r] = ' % node.target)
		self.write('env.get_template(')
		self.visit(node.template, frame)
		self.write(', %s).' % json.dumps(self.name))
		if node.with_context:
			self.writeline('make_module(context.parent, true, locals());')
		else:
			self.writeline('module;')
		if frame.toplevel and not node.target.startswith('_'):
			self.writeline('context.exported_vars.discard(%r)' % node.target)
		frame.assigned_names.add(node.target)

	def visit_FromImport(self, node, frame):
		"""Visit named imports."""
		self.write('included_template = environment.get_template(')
		self.visit(node.template, frame)
		self.write(', %r).' % self.name)
		if node.with_context:
			self.write('make_module(context.parent, True)')
		else:
			self.write('module')

		var_names = []
		discarded_names = []
		for name in node.names:
			if isinstance(name, tuple):
				name, alias = name
			else:
				alias = name
			self.writeline('l_%s = getattr(included_template, '
						   '%r, missing)' % (alias, name))
			self.writeline('if l_%s is missing:' % alias)
			self.indent()
			self.writeline('l_%s = environment.undefined(%r %% '
						   'included_template.__name__, '
						   'name=%r)' %
						   (alias, 'the template %%r (imported on %s) does '
						   'not export the requested name %s' % (
								self.position(node),
								repr(name)
						   ), name))
			self.outdent()
			if frame.toplevel:
				var_names.append(alias)
				if not alias.startswith('_'):
					discarded_names.append(alias)
			frame.assigned_names.add(alias)

		if var_names:
			if len(var_names) == 1:
				name = var_names[0]
				self.writeline('context.vars[%r] = l_%s' % (name, name))
			else:
				self.writeline('context.vars.update({%s})' % ', '.join(
					'%r: l_%s' % (name, name) for name in var_names
				))
		if discarded_names:
			if len(discarded_names) == 1:
				self.writeline('context.exported_vars.discard(%r)' %
							   discarded_names[0])
			else:
				self.writeline('context.exported_vars.difference_'
							   'update((%s))' % ', '.join(map(repr, discarded_names)))

	def visit_For(self, node, frame):
		# when calculating the nodes for the inner frame we have to exclude
		# the iterator contents from it
		children = node.iter_child_nodes(exclude=('iter',))
		if node.recursive:
			loop_frame = self.function_scoping(node, frame, children,
											   find_special=False)
		else:
			loop_frame = frame.inner()
			loop_frame.inspect(children)

		# try to figure out if we have an extended loop.  An extended loop
		# is necessary if the loop is in recursive mode if the special loop
		# variable is accessed in the body.
		extended_loop = node.recursive or 'loop' in \
						find_undeclared(node.iter_child_nodes(
							only=('body',)), ('loop',))

		# if we don't have an recursive loop we have to find the shadowed
		# variables at that point.  Because loops can be nested but the loop
		# variable is a special one we have to enforce aliasing for it.
		if not node.recursive:
			aliases = self.push_scope(loop_frame, ('loop',))

		# otherwise we set up a buffer and add a function def
		else:
			self.writeline('def loop(reciter, loop_render_func):', node)
			self.indent()
			self.buffer(loop_frame)
			aliases = {}

		# make sure the loop variable is a special one and raise a template
		# assertion error if a loop tries to write to loop
		if extended_loop:
			loop_frame.identifiers.add_special('loop')
		for name in node.find_all(nodes.Name):
			if name.ctx == 'store' and name.name == 'loop':
				self.fail('Can\'t assign to special loop variable '
						  'in for-loop target', name.lineno)

		self.pull_locals(loop_frame)
		iter_var = self.temporary_identifier()
		target_var = self.temporary_identifier()
		self.write('var %s, %s = ' % (iter_var, target_var))
		self.visit(node.target, loop_frame)
		self.writeline(';')

		if node.else_:
			iteration_indicator = self.temporary_identifier()
			self.writeline('%s = 1;' % iteration_indicator)

		# Create a fake parent loop if the else or test section of a
		# loop is accessing the special loop variable and no parent loop
		# exists.
		if 'loop' not in aliases and 'loop' in find_undeclared(
		   node.iter_child_nodes(only=('else_', 'test')), ('loop',)):
			self.writeline("l_loop = environment.undefined(%r, name='loop')" %
				("'loop' is undefined. the filter section of a loop as well "
				 "as the else block doesn't have access to the special 'loop'"
				 " variable of the current loop.  Because there is no parent "
				 "loop it's undefined.  Happened in loop on %s" %
				 self.position(node)))

		self.write('for (%s = 0; %s < %s.length; %s++) {' % (iter_var, iter_var, target_var, iter_var))

		# if we have an extened loop and a node test, we filter in the
		# "outer frame".
		if extended_loop and node.test is not None:
			self.write('(')
			self.visit(node.target, loop_frame)
			self.write(' for ')
			self.visit(node.target, loop_frame)
			self.write(' in ')
			if node.recursive:
				self.write('reciter')
			else:
				self.visit(node.iter, loop_frame)
			self.write(' if (')
			test_frame = loop_frame.copy()
			self.visit(node.test, test_frame)
			self.write('))')

		elif node.recursive:
			self.write('reciter')
		else:
			pass
			#self.visit(node.iter, loop_frame)

		if node.recursive:
			self.writeline(', recurse=loop_render_func)) {')
		else:
			pass
			#self.writeline(extended_loop and ')) {' or ') {')

		# tests in not extended loops become a continue
		if not extended_loop and node.test is not None:
			self.write('if (!(')
			self.visit(node.test, loop_frame)
			self.writeline(')) {')
			self.writeline('continue;')
			self.writline('}')

		self.blockvisit(node.body, loop_frame)
		if node.else_:
			self.writeline('%s = 0;' % iteration_indicator)

		self.writeline('}')

		if node.else_:
			self.writeline('if (%s) {' % iteration_indicator)
			self.blockvisit(node.else_, loop_frame)
			self.writeline('}')

		# reset the aliases if there are any.
		if not node.recursive:
			self.pop_scope(aliases, loop_frame)

		# if the node was recursive we have to return the buffer contents
		# and start the iteration code
		if node.recursive:
			self.return_buffer_contents(loop_frame)
			self.outdent()
			self.start_write(frame, node)
			self.write('loop(')
			self.visit(node.iter, frame)
			self.write(', loop)')
			self.end_write(frame)

	def visit_If(self, node, frame):
		if_frame = frame.soft()
		self.write('if (')
		self.visit(node.test, if_frame)
		self.writeline(') {')
		self.blockvisit(node.body, if_frame)
		if node.else_:
			self.writeline('} else {')
			self.blockvisit(node.else_, if_frame)
		self.writeline('}')

	def visit_Macro(self, node, frame):
		self.write('var l_%s = ' % node.name)
		macro_frame = self.macro_body(node, frame)
		if frame.toplevel:
			if not node.name.startswith('_'):
				self.writeline('context.exported_vars.add(%r);' % node.name)
			self.write('context.vars[%r] = ' % node.name)
		self.macro_def(node, macro_frame)
		frame.assigned_names.add(node.name)

	def visit_Output(self, node, frame=None):
		if self.has_known_extends and frame.require_output_check:
			return

		outdent_later = False
		if frame.require_output_check:
			self.writeline('if (parentTemplate === undefined) {')
			outdent_later = True

		body = []
		for child in node.nodes:
			try:
				const = child.as_const()
			except nodes.Impossible:
				body.append(child)
				continue
			try:
				if self.environment.autoescape:
					if hasattr(const, '__html__'):
						const = const.__html__()
					else:
						const = escape(const)
				const = unicode(const)
			except Exception, e:
				# if something goes wrong here we evaluate the node
				# at runtime for easier debugging
				body.append(child)
				continue
			if body and isinstance(body[-1], list):
				body[-1].append(const)
			else:
				body.append([const])

		self.write('stream.push(')
		for item in body:
			if isinstance(item, list):
				val = json.dumps(concat(item))
				self.write(val)
			else:
				self.visit(item, frame)
			if item != body[-1]:
				self.writeline(', ')
		self.writeline(');')

		if outdent_later:
			self.writeline('}')

	def visit_Assign(self, node, frame):
		# toplevel assignments however go into the local namespace and
		# the current template's context.  We create a copy of the frame
		# here and add a set so that the Name visitor can add the assigned
		# names here.
		if frame.toplevel:
			assignment_frame = frame.copy()
			assignment_frame.toplevel_assignments = set()
		else:
			assignment_frame = frame
		self.visit(node.target, assignment_frame)
		self.write(' = ')
		self.visit(node.node, frame)
		self.writeline(';')

		# make sure toplevel assignments are added to the context.
		if frame.toplevel:
			public_names = [x for x in assignment_frame.toplevel_assignments
							if not x.startswith('_')]
			if len(assignment_frame.toplevel_assignments) == 1:
				name = next(iter(assignment_frame.toplevel_assignments))
				self.writeline('context.vars[%r] = l_%s;' % (name, name))
			else:
				self.writeline('context.vars.update({')
				for idx, name in enumerate(assignment_frame.toplevel_assignments):
					if idx:
						self.write(', ')
					self.write('%r: l_%s' % (name, name))
				self.write('});')
			if public_names:
				if len(public_names) == 1:
					self.writeline('context.exported_vars.add(%r);' %
								   public_names[0])
				else:
					self.writeline('context.exported_vars.update((%s));' %
								   ', '.join(map(repr, public_names)))

	def visit_Name(self, node, frame):
		if node.ctx == 'store' and frame.toplevel:
		    frame.toplevel_assignments.add(node.name)
		self.write('l_' + node.name)
		frame.assigned_names.add(node.name)

	def visit_Const(self, node, frame):
		val = node.value
		self.write(json.dumps(val))

	def visit_TemplateData(self, node, frame):
		self.write(json.dumps(node.as_const()))

	def visit_Tuple(self, node, frame):
		self.write('(')
		idx = -1
		for idx, item in enumerate(node.items):
		    if idx:
		        self.write(', ')
		    self.visit(item, frame)
		self.write(idx == 0 and ',)' or ')')

	def visit_List(self, node, frame):
		self.write('[')
		for idx, item in enumerate(node.items):
		    if idx:
		        self.write(', ')
		    self.visit(item, frame)
		self.write(']')

	def visit_Dict(self, node, frame):
		self.write('{')
		for idx, item in enumerate(node.items):
		    if idx:
		        self.write(', ')
		    self.visit(item.key, frame)
		    self.write(': ')
		    self.visit(item.value, frame)
		self.write('}')

	def visit_Getattr(self, node, frame):
		self.write('env.getattr(')
		self.visit(node.node, frame)
		self.write(', %r)' % node.attr)

	def visit_Getitem(self, node, frame):
		# slices bypass the environment getitem method.
		if isinstance(node.arg, nodes.Slice):
			self.visit(node.node, frame)
			self.write('[')
			self.visit(node.arg, frame)
			self.write(']')
		else:
			self.write('env.getitem(')
			self.visit(node.node, frame)
			self.write(', ')
			self.visit(node.arg, frame)
			self.write(')')

	def visit_Slice(self, node, frame):
		if node.start is not None:
			self.visit(node.start, frame)
		self.write(':')
		if node.stop is not None:
			self.visit(node.stop, frame)
		if node.step is not None:
			self.write(':')
			self.visit(node.step, frame)

	def visit_Filter(self, node, frame):
		self.write(self.filters[node.name] + '(')
		func = self.environment.filters.get(node.name)
		if func is None:
			self.fail('no filter named %r' % node.name, node.lineno)
		if getattr(func, 'contextfilter', False):
			self.write('context, ')
		elif getattr(func, 'environmentfilter', False):
			self.write('env, ')

		# if the filter node is None we are inside a filter block
		# and want to write to the current buffer
		if node.node is not None:
			self.visit(node.node, frame)
		elif self.environment.autoescape:
			self.write('Markup(concat(%s))' % frame.buffer)
		else:
			self.write('concat(%s)' % frame.buffer)
		self.signature(node, frame)
		self.write(')')

	def visit_Test(self, node, frame):
		self.write(self.tests[node.name] + '(')
		if node.name not in self.environment.tests:
			self.fail('no test named %r' % node.name, node.lineno)
		self.visit(node.node, frame)
		self.signature(node, frame)
		self.write(')')

	def visit_CondExpr(self, node, frame):
		def write_expr2():
			if node.expr2 is not None:
				return self.visit(node.expr2, frame)
			self.write('environment.undefined(%r)' % ('the inline if-'
					   'expression on %s evaluated to false and '
					   'no else section was defined.' % self.position(node)))

		if not have_condexpr:
			self.write('((')
			self.visit(node.test, frame)
			self.write(') and (')
			self.visit(node.expr1, frame)
			self.write(',) or (')
			write_expr2()
			self.write(',))[0]')
		else:
			self.write('(')
			self.visit(node.expr1, frame)
			self.write(' if ')
			self.visit(node.test, frame)
			self.write(' else ')
			write_expr2()
			self.write(')')

class Jinjs(object):
	def __init__(self, environment):
		self.environment = environment
		self.loader = environment.loader

	def get_all_templates(self):
		if isinstance(loader, loaders.DictLoader):
			for name in self.loader.mapping.iterkeys():
				yield name

	def generate(self, templates=None):
		for name in templates if templates is not None else self.get_all_templates():
			template, filename, uptodate = self.loader.get_source(self.environment, name)
			generator = CodeGenerator(self.environment, name, filename)
			source = env.parse(template, name, filename)
			generator.visit(source)
			yield generator.stream.getvalue(), name, filename

	def compile(self, var='Jinjs.templates'):
		stream = StringIO()
		for code, name, filename in self.generate():
			stream.write('%s[%s] = %s;\n' % (var, json.dumps(name), code))
		return stream.getvalue()

class Environment(jinja2.Environment):
	def _generate(self, source, name, filename, defer_init=False):
		generator = CodeGenerator(self, name, filename)
		generator.visit(source)
		return generator.stream.getvalue()

	def _compile(self, source, filename):
		return source

class Template(jinja2.Template):
	ctx = pyv8.JSContext()
	ctx.enter()
	ctx.eval(open(os.path.join(os.path.dirname(__file__), 'jinjs.js')).read())

	@classmethod
	def from_code(cls, environment, code, globals, uptodate=None):
		obj = cls.ctx.eval(code)
		t = object.__new__(cls)
		t.environment = environment
		t.name = obj.name
		t.globals = globals
		def root(context):
			stream = pyv8.JSArray([])
			obj.renderFunc(environment, context, stream)
			return list(stream)
		t.root_render_func = root
		t.renderFunc = obj.renderFunc
		t._module = None
		t.blocks = dict(obj.blocks)
		t._uptodate = uptodate
		return t

	def new_context(self, vars=None, shared=False, locals=None):
		ctx = super(Template, self).new_context(vars, shared, locals)
		ctx.blocks = dict((k, pyv8.JSArray(v)) for k, v in ctx.blocks.iteritems())
		return ctx

old_resolve = Context.resolve
def resolve(self, *args, **kwargs):
	try:
		result = old_resolve(self, *args, **kwargs)
	except Exception, e:
		result = None
	if isinstance(result, Undefined):
		return ''
	return result
Context.resolve = resolve

Environment.template_class = Template

def run_tests(test):
	import unittest

	SavedEnvironment = jinja2.Environment
	jinja2.Environment = Environment

	__import__(test)

	unittest.main(defaultTest=test)

	jinja2.Environment = SavedEnvironment

if __name__ == '__main__':
	#run_tests('jinja2.testsuite.imports')

	"""
	import os

	with pyv8.JSContext() as ctx:
		ctx.eval(open(os.path.join(os.path.dirname(__file__), 'jinjs.js')).read())

		loader = loaders.DictLoader({
			'a': '{% if false %}{% block x %}A{% endblock %}{% endif %} C D',
			'b': '{% extends "a" %}{% block x %}B{% endblock %}'
		})

		env = jinja2.Environment(loader=loader)

		jinjs = Jinjs(env)
		print jinjs.compile()
		ctx.eval(jinjs.compile())

		print ctx.eval('Jinjs(Jinjs.templates["b"], {})')
	"""

	from jinja2.testsuite.inheritance import env
	from jinja2 import DictLoader
	loader = env.loader
	loader = DictLoader({
		'master': '''
			{% for i in [1, 2, 3, 4] %}
				Hello, {{ i }}!
			{% endfor %}
		''',
		'a': '''\
{% macro say_hello(name) %}Hello {{ name }}!{% endmacro %}
{{ say_hello('Peter') }}'''
	})

	env1 = jinja2.Environment(loader=loader, trim_blocks=True)
	env2 = Environment(loader=loader, trim_blocks=True)

	name = 'a'

	tmpl = loader.get_source(env, name)[0]

	print '===============TEMPLATE==============='
	print tmpl

	print '\n\n\n===============PARSED==============='
	print env1.parse(tmpl)

	print '\n\n\n===============PYTHON==============='
	print env1.compile(env1.parse(tmpl), name, None, raw=True)

	print '\n\n\n===============JAVASCRIPT==============='
	print env2.compile(env2.parse(tmpl), name, None, raw=True)

	print '\n\n\n===============RENDERED1==============='
	print env1.get_template(name).render()

	print '\n\n\n===============RENDERED2==============='
	print env2.get_template(name).render()
