JinJS: Jinja to Javascript template compiler
============================================

JinJS is a project which provides a Jinja2-compatible template to Javascript 
compiler. This allows projects to use the same templates from client-side 
code.

Building
--------

JinJS depends on Jinja2 version 2.5 or later (specifically, it requires the 
changes that were introduced in [changeset 
709fe2a94004](http://dev.pocoo.org/hg/jinja2-main/rev/709fe2a94004) for [ticket 
#384](http://dev.pocoo.org/projects/jinja/ticket/384)), as well as 
[PyV8](http://code.google.com/p/pyv8/) for running the test suite.

