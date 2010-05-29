var Jinjs = function(template, context, environment) {
	if (environment === undefined) {
		environment = Jinjs.defaultEnvironment;
	}

	context = new Jinjs.Context(context);
	template = new Jinjs.Template(environment, template);

	return template.render(context);
};

Jinjs.Macro = function(env, func, name, args) {
	
};

Jinjs.templates = {};

Jinjs.Environment = function(loader) {
	this.loader = loader === undefined ? Jinjs.templates : loader;
};

Jinjs.Environment.prototype.get_template = function(name) {
	return this.loader[name];
};

Jinjs.Template = function(environment, template) {
	this.environment = environment;
	this.template = template;
	this.renderFunc = template.renderFunc;
	this.blocks = template.blocks;
};

Jinjs.Template.prototype.render = function(context) {
	var result = [];
	this.renderFunc(this.environment, context, result);
	return result.join();
};

Jinjs.defaultEnvironment = new Jinjs.Environment();

Jinjs.Context = function(context) {
	this.context = context;
	this.blocks = {};
};

Jinjs.Context.prototype['super'] = function() {
	
};

Jinjs.BlockReference = function(context) {
	var obj = function() {
	
	};

	obj['super'] = function() {
	
	};

	return obj;
};

Jinjs.TemplateReference = function(context) {
	for (var name in context.blocks) {
		if (context.blocks.hasOwnProperty(name)) {
			this[name] = context.blocks[name];
		}
	}
};
