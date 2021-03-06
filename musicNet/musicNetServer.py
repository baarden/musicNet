#!/usr/bin/python
#-------------------------------------------------------------------------------
# Name:         musicNetServer.py
# Purpose:      a Python app providing a RESTful interface 
#               to the musicNet classes
#
# Authors:      Bret Aarden
# Version:      0.2
# Date:         22Dec2012
#
# License:      MPL 2.0
#-------------------------------------------------------------------------------


'''
This is an app that provides a RESTful JSON interface to the music21.musicNet objects.
Any client with the ability to send/receive JSON can use a remote
server running this app to execute queries and return the results. It's dependent on
the Python Flask, py2neo, redis, and urimagic modules. (Doctests are dependent on the webtest module.)

Services with a result containing multiple rows return each row as a separate JSONItem:
rather than returning one JSON document containing an array, each line
is a separate JSON document containing one row. Any service may return
a JSONItem dictionary with an 'error'/error message key/value pair, so
clients should check for this possibility.

Only query-related services are exposed by this interface. Actions that manipulate 
the database can only be done on the server using the musicNet objects directly.

The app is built with the Flask framework and will start using its default options
(localhost:5000). A Redis server is expected at its default location (localhost:6379).
Redis is used to communicate with the separate multiprocessing workers that generate
preview images.

Services
========
'''

import os
import sys
import time
import json
import pickle
import tempfile
import music21
import music21.musicNet
import py2neo
import redis
import flask

app = flask.Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024
app.db = music21.musicNet.Database()
app.redis = redis.StrictRedis(host='localhost', port=6379, db=0)
app.tokens = {}
app.rGens = {}
app.images = {}


def _getPy2neoMetadata(node):
    ''' Returns the hidden metadata of a py2neo object, 
    the location of which can change depending on the version of Neo4j.
    '''
    # 1.4: return node._Resource__metadata['data'] # 1.4
    return node.columns

'''
listScores
----------
'''

@app.route('/listscores')
def listScores():
    '''
    Server address::
 
        /listscores?start=0&limit=100
    
    Request parameters::
    
        start - The first row of scores to return (default=0).
        limit - The number of scores to return (default=100).
    
    Response: a JSONItem dictionary for each score in the database.
    
    Data structure::

        { movementName: name_of_score_file, _names: [ contributor, ... ], index: original_path_of_score_file }
        { movementName: name_of_score_file, _names: [ contributor, ... ], index: original_path_of_score_file }
        ...
    
    We can test the functionality of the app without actually starting it by using the :class:`webtest`
    framework:
    
    >>> from webtest import TestApp
    >>> import music21.musicNet.musicNetServer as mns
    >>> webapp = TestApp(mns.app)
    >>> r = webapp.get('/listscores')
    >>> import json
    >>> sorted(json.loads(r.body).items())
    [(u'_names', [None]), (u'corpusFilepath', u'bach/bwv84.5.mxl'), (u'movementName', u'bwv84.5.mxl')]
    '''
    query = flask.request.args
    start = int(query.get('start', '0'))
    limit = int(query.get('limit', '100'))
    rows = app.db.listScores(start, limit)
    for row in rows:
        yield json.dumps(row) + '\n'

'''
listNodeTypes
-------------
'''     
@app.route('/listnodetypes')
def listNodeTypes():
    '''    
    Server address::
 
        /listnodetypes
    
    Request parameters: none.
    
    Response: a JSON array of node types in the database.
    
    Data structure::

        [ 'score', 'metadata', 'note', ... ]
        
    Example:

    >>> from webtest import TestApp
    >>> import music21.musicNet.musicNetServer as mns
    >>> webapp = TestApp(mns.app)
    >>> r = webapp.get('/listnodetypes')
    >>> import json
    >>> json.loads(r.body)
    [u'StaffGroup', u'SystemLayout', u'Beam', u'Score', u'Rest', u'Note', u'Instrument', u'Moment', u'Part', u'Measure', u'Barline', u'Expression', u'Metadata']
    '''
    types = list(app.db.listNodeTypes())
    return json.dumps(types) + '\n'

@app.route('/listnodeproperties')
def listNodeProperties():
    '''
    Server address::
 
        /listnodeproperties
    
    Request parameters: none.
    
    Response: 
    
        A JSON array for each node property type in the database. The first element is the node
        type, and the second element is the name of the property.
        
    Data structure::
    
        ['StaffGroup', 'offset']
        ['SystemLayout', 'distance']
        ['SystemLayout', 'quarterLength']
        ...
    
    Example:

    >>> from webtest import TestApp
    >>> import music21.musicNet.musicNetServer as mns
    >>> webapp = TestApp(mns.app)
    >>> r = webapp.get('/listnodeproperties')
    >>> import json
    >>> json.loads(r.body.splitlines()[0])
    [u'StaffGroup', u'offset']
    '''
    rows = app.db.listNodeProperties()
    for row in rows:
        yield json.dumps(row) + '\n'

@app.route('/listnodepropertyvalues')
def listNodePropertyValues():
    '''
    Server address::
 
        /listnodepropertyvalues
    
    Request parameters: none.
    
    Response: 
    
        A JSON array for each node property type in the database. The first element is the node
        type, the second element is the name of the property, and the third is a list of values for that
        property found in the database.
        
    Data structure::
    
        ['Instrument', 'partName', ['Alto', 'Bass', 'Soprano', 'Tenor']]
        ['Note', '_stemDirection', ['down', 'up']]
        ...
    
    Example:

    >>> from webtest import TestApp
    >>> import music21.musicNet.musicNetServer as mns
    >>> webapp = TestApp(mns.app)
    >>> r = webapp.get('/listnodepropertyvalues')
    >>> import json
    >>> json.loads(r.body.splitlines()[0])
    [u'StaffGroup', u'offset', [0.0,]]
    '''
    rows = app.db.listNodePropertyValues()
    def generate():
        for row in rows:
            yield json.dumps(row) + '\n'        
    return flask.Response(generate(), mimetype='application/json')
        
@app.route('/listrelationshiptypes')
def listRelationshipTypes():
    '''
    Server address::
 
        /listrelationshiptypes
    
    Request parameters: none.
    
    Response: 
    
        A JSONItem dictionary for each kind of relationship in the database, including
        the type of node at the start and end of the relationship.
    
    Data structure::
    
        {'start': u'Note', 'end': u'Measure', 'type': u'NoteInMeasure'}
        {'start': u'Part', 'end': u'Score', 'type': u'PartInScore'}
        ...
    
    Example:

    >>> from webtest import TestApp
    >>> import music21.musicNet.musicNetServer as mns
    >>> webapp = TestApp(mns.app)
    >>> r = webapp.get('/listrelationshiptypes')
    >>> import json
    >>> json.loads(r.body.splitlines()[0])
    {u'start': u'Note', u'end': u'Measure', u'type': u'NoteInMeasure'}
    '''
    rows = app.db.listRelationshipTypes()
    def generate():
        for row in rows:
            yield json.dumps(row) + '\n'
    return flask.Response(generate(), mimetype='application/json')

@app.route('/listrelationshipproperties')
def listRelationshipProperties():
    '''
    Server address::
 
        /listrelationshipproperties
    
    Request parameters: none.
    
    Response: 
    
        A 2-element JSONItem array for each kind of Relationship property in the database:
        the first item is the Relationship type, and the second is the name of the property.
    
    Data structure::
    
        ['NoteToNoteByBeat', 'interval']
        ['NoteToNote', 'interval']
        ...
        
    Example:

    >>> from webtest import TestApp
    >>> import music21.musicNet.musicNetServer as mns
    >>> webapp = TestApp(mns.app)
    >>> r = webapp.get('/listrelationshiptypes')
    >>> import json
    >>> sorted(json.loads(r.body.splitlines()[0]).items())
    [(u'end', u'Measure'), (u'start', u'Note'), (u'type', u'NoteInMeasure')]
    '''
    rows = app.db.listRelationshipProperties()
    def generate():
        for row in rows:
            yield json.dumps(row) + '\n'
    return flask.Response(generate(), mimetype='application/json')

@app.route('/listrelationshippropertyvalues')
def listRelationshipPropertyValues():
    '''
    Server address::
 
        /listrelationshippropertyvalues
    
    Request parameters: none.
    
    Response: 
    
        A JSON array for each relationship property type in the database. The first element is the relationship
        type, the second element is the name of the property, and the third is a list of values for that
        property found in the database.
        
    Data structure::
    
        ['NoteToNoteByBeat', 'interval', [-12, -7, -5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 7, 12]]
        ['NoteToNote', 'interval', [-12, -7, -5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 7, 12]]
        ...
    
    Example:

    >>> from webtest import TestApp
    >>> import music21.musicNet.musicNetServer as mns
    >>> webapp = TestApp(mns.app)
    >>> r = webapp.get('/listrelationshippropertyvalues')
    >>> import json
    >>> json.loads(r.body.splitlines()[0])
    [u'NoteToNoteByBeat', u'interval', [-12, -7, -5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 7, 12]]
    '''
    rows = app.db.listRelationshipPropertyValues()
    def generate():
        for row in rows:
            yield json.dumps(row) + '\n'
    return flask.Response(generate(), mimetype='application/json')

@app.route('/images/<filename>.png')
def sendImage(filename):
    tempdir = tempfile.gettempdir()
    return flask.send_file(tempdir + '/' + filename + '.png', mimetype='image/png')
    #return bottle.static_file(filename, root=tempdir, mimetype='image/png')

@app.route('/submitquery', methods=['POST'])
def prepareQuery():
    '''
    Server address::
 
        /submitquery
    
    This service processes a JSON query and returns an integer token that can be used
    to obtain results using 'getresults' and 'getimages'. Unlike
    the other services, it uses the POST method and expects the body of the request
    to be a JSON document containing the query. Tokens are good for a
    half hour before they expire. Identical queries from the same IP address will
    return the same token.
    
    The JSON query is expected to be an object with specific key/value pairs:
    
      nodes
        An array of objects, one per node, each identifying its 
        'type' and its 'name' in the query.
      relationships
        An array of objects, one per relationship, each identifying its 'type', 
        the name of its 'start' node, the name of its 'end' node, and its own 'name' 
        in the query.
      startNode
        The name of the starting node for the query.
      startRelationship
        The name of the starting relationship for the query. There must be 
        exactly one startNode or startRelationship per query. Results are
        ordered by the ID of the start object.
      relationshipProperties
        An array of objects, one per relationship property that is referenced
        in the query, each identifying the name of the 'relationship', the
        relationship 'property', and its 'name' in the query.
      nodeProperties
        An array of objects, one per node property that is referenced
        in the query, each identifying the name of the 'node', the
        node 'property', and its 'name' in the query.
      comparisonFilters
        An array of objects, one per comparison filter. Each object should specify
        the comparison 'operator' (one of `=', '<>', '<', '>', '<=',` or '>='),
        the comparison prefix ('pre' and 'preType') and comparison postfix
        ('post' and 'postType'). The pre/post type can be either a 'property' (a property
        name) or a 'value' (a number, string, or boolean). 
      returns
        An array of property names that will be returned by the query. 
      makePreviews
        A boolean indicating whether previews will be created. Queries that
        specify returns are not compatible with the 'makePreviews' option.
        Preview URLs can be obtained from the getimage service.
    
    For instance::
    
        { 'nodes': [ { 'type': 'Note', 'name': 'n1' },
                     { 'type': 'Note', 'name': 'n2' } ],
          'relationships': [ { 'start': 'n1', 'type': 'NoteSimultaneousWithNote', 'end': 'n2', 'name': 'nswn' } ],
          'startNode': 'n1',
          'relationshipProperties': [ { 'relationship': 'nswn', 'property': 'sameOffset', 'name': 'nswnSo' },
                                      { 'relationship': 'nswn', 'property': 'simpleHarmonicInterval', 'name': 'nswnShi' } ],
          'comparisonFilters': [ { 'preType': 'property', 'pre': 'nswnSo', 'operator': '=', 'postType': 'value', 'post': 'True' },
                                 { 'preType': 'property', 'pre': 'nswnShi', 'operator': '=', 'postType': 'value', 'post': 7 } ],
          'returns': [],
          'makePreviews': 'True'
        }
        
    A NoteToNote relationship can have a special MaxDistanceBetweenNotes property; when set '=' to
    a positive integer in a comparison filter, this will create a variable-length relationship.
    
    Data structure::
    
        {'token': -4258737148674360350}
        
    Example:

    >>> import webtest
    >>> import music21.musicNet.musicNetServer as mns
    >>> webapp = webtest.TestApp(mns.app)
    >>> req = { 'nodes': [ { 'type': 'Note', 'name': 'n1' } ], 'startNode': 'n1' }
    >>> import json
    >>> r = webapp.post_json('/submitquery', req)
    >>> json.loads(r.body)
    {u'token': ...}
    '''
    req = flask.request.get_json()
    q = music21.musicNet.Query(app.db)
    scoreNode = None
    nodes = {}
    relations = {}
    properties = {}
    measuresForNotes = {}
    partsForMeasures = {}
    instrumentsForParts = {}
    scoresForParts = {}
    notes = []
    columns = []
    print req
    if not req.get('nodes', False):
        return { 'error': 'query has no nodes' }
    for n in req['nodes']:  
        node = q.addNode(n['type'], n['name'])
        nodes[n['name']] = node
        if (n['type'] == 'Note'):
            notes.append(node)
            columns.append(n['name'])
        elif (n['type'] == 'Score'):
            scoreNode = node
        elif (n['type'] in ('Measure', 'Part')):
            columns.append(n['name'])
    if scoreNode == None:
        scoreNode = q.addNode('Score', 'Score')
    for r in req.get('relationships', []):
        start = nodes[r['start']]
        end = nodes[r['end']]
        optional = getattr(r, 'optional', None)
        relation = q.addRelationship(relationType=r['type'], start=start, end=end, 
                                     name=r['name'], optional=optional)
        relations[r['name']] = relation
        if (r['type'] == 'NoteInMeasure'):
            measuresForNotes[start] = end
        elif (r['type'] == 'MeasureInPart'):
            partsForMeasures[start] = end
        elif (r['type'] == 'InstrumentInPart'):
            instrumentsForParts[end] = start 
        elif (r['type'] == 'PartInScore'):
            scoresForParts[start] = end
    if 'startNode' in req:
        q.setStartNode(nodes[req['startNode']])
    elif 'startRelationship' in req:
        q.setStartRelationship(relations[req['startRelationship']])
    else:
        return { 'error': 'query has no start' }
    if 'nodeProperties' in req:
        for p in req['nodeProperties']:
            node = nodes[p['node']]
            properties[p['name']] = getattr(node, p['property'])
    if 'relationshipProperties' in req:
        for p in req['relationshipProperties']:
            relation = relations[p['relationship']]
            properties[p['name']] = getattr(relation, p['property'])
    if 'comparisonFilters' in req:
        for f in req['comparisonFilters']:
            addComparisonFilter(q, f, properties)
    if 'returns' in req and req['returns']:
        returns = [properties[r['property']] for r in req['returns']]
        columns.extend(returns);
        q.addReturns(*returns)
#    addPartConstraints(q, parts)
    for n in notes:
        if n not in measuresForNotes.keys():
            m = q.addNode('Measure', 'measure_' + n.name)
            columns.append(m.name)
            r = q.addRelationship(relationType='NoteInMeasure', start=n, end=m, name=n.name+'In'+m.name, optional=True)
            measuresForNotes[n] = m
#    for m in measuresForNotes.values():
#        if m not in partsForMeasures.keys():
#            p = q.addNode('Part', 'part_' + m.name)
#            r = q.addRelationship(relationType='MeasureInPart', start=m, end=p, name=m.name+'In'+p.name, optional=True)
#            partsForMeasures[m] = p
    for p in partsForMeasures.values():
        if p not in instrumentsForParts.keys():
            i = q.addNode('Instrument', 'instrument_' + p.name)
            r = q.addRelationship(relationType='InstrumentInPart', start=i, end=p, name=i.name+'In'+p.name, optional=True)
    if len(scoresForParts) == 0:
        p = partsForMeasures.itervalues().next()
        r = q.addRelationship(relationType='PartInScore', start=p, end=scoreNode, name=p.name+'In'+scoreNode.name)
    previews = req.get('makePreviews', False)
    pattern = q._assemblePattern(distinct=True)
    if not pattern:
        result = {'error': 'Unable to assemble valid query.'}
    else:
        print pattern ###
        ipAddr = flask.request.remote_addr or "None"
        token = hash(ipAddr + pattern)
        rGen = music21.musicNet.Results(pattern)
        rGen.start()
        app.rGens[token] = rGen
        app.tokens[token] = [pattern, columns, previews, time.time()]
        result = { 'token': token }
    expireTokens()
    print 'result: ' + str(result) ###
    response = json.dumps(result)
    return response

#def addPartConstraints(q, parts, properties):
#    partList = parts.values()
#    for i in range(len(partList) - 1):
#        for j in range(1, len(partList)):
#            filter = {'preType': 'property',
#                      'pre': partList[i] + 'number',
#                      'postType': 'property',
#                      'post': partList[j] + 'number',
#                      'operator': 1}
#            addComparisonFilter(q, filter, properties)

@app.route('/cancelquery')
def cancel_query():
    '''
    Server address::
     
        /cancelquery?token=nnn
    
    Request parameters::

        token - The number returned by the submitquery service
    
    Response: 
    
        None. Attempts to cancel the query associated with the given token.
    '''
    print 'getting cancel request'
    token = int(flask.request.args.get('token', ''))
    try:
        rGen = app.rGens[token]
    except KeyError:
        print 'Failed to cancel.'
        return
    rGen.stop()
    print 'Canceling!'
    del app.rGens[token]

@app.route('/getresults')
def results():
    '''
    Server address::
     
        /getresults?token=nnn&row=0&limit=10
    
    Request parameters::

        token - The number returned by the submitquery service
        row   - The first row of the query to return (default=0)
        limit - The number of query results to return (default=10)
    
    Response: 
    
        A series of JSONItem dictionaries: first the metadata for the
        results (the list of column names), then the data rows.
        A 'type' key indicates the kind of row, and the information
        is indexed by the 'data' key. Data rows also have a 'index'
        key indicating the line number of the result.
        
        A typical strategy is to send requests to this service,
        incrementing the row by the limit each time, until it
        returns an empty response.
    
    Data structure::
    
        {'type': 'metadata', 'data': ['nswn', 'p2', 'n1', 'p1', 'ntn1', 'n2', 'ntn2', 'p1', 'm1']}
        {'type': 'data', 'index': 0, 'data': [7, 'D4', 'F#4', 'B4', -5, 'B3', -3, 'Soprano', 'Tenor', 5]}
        {'type': 'data', 'index': 1, 'data': [19, 'B3', 'C#5', 'D5', -1, 'F#3', -5, 'Soprano', 'Bass', 6]}
        ...
        
    Example:
        
    >>> import webtest
    >>> import music21.musicNet.musicNetServer as mns
    >>> webapp = webtest.TestApp(mns.app)
    >>> req = { 'nodes': [ { 'type': 'Note', 'name': 'n1' } ], 'startNode': 'n1' }
    >>> r = webapp.post_json('/submitquery', req)
    >>> import json
    >>> token = json.loads(r.body)['token']
    >>> r = webapp.get('/getresults?token=%s&start=0&limit=1' % token)
    >>> sorted(json.loads(r.body.splitlines()[0]).items())
    [(u'data', [u'n1']), (u'type', u'metadata')]
    '''
    args = flask.request.args
    token = int(args.get('token', ''))
    minRow = int(args.get('row', '0'))
    limit = int(args.get('limit', '10'))
    return flask.Response(generate_results(token, minRow, limit), mimetype='application/json')

def generate_results(token, minRow, limit):
    try:
        rGen = app.rGens[token]
        pattern, columns, previews, timestamp = app.tokens[token]
    except KeyError:
        item = { 'error': 'That token is invalid or has expired.' }
        yield json.dumps(item) + '\n'
        raise StopIteration
    data = rGen.next(limit)
    if not data:
        raise StopIteration
    metadata = _getPy2neoMetadata(data[0])
    #data[0]._fields
    print metadata
    data = [tuple(x) for x in data]
    rLookup = {}
    for i in range(len(metadata)):
        rLookup[metadata[i]] = i
    for idx in range(len(data)):
        queryIdx = idx + minRow
        # remove any None values from the output
        row = data[idx]
        print row
        output, metaOutput, previewData = makeScore(row, metadata, columns, previews)
        if idx == 0:
            item = { 'type': 'metadata', 'data': metaOutput }
            yield json.dumps(item) + '\n'
        if previews:
            previewData['index'] = queryIdx
            previewData['token'] = token
            pvals = pickle.dumps(previewData)
            app.redis.rpush('inQueue', pvals)
        item = { 'type': 'data', 'index': queryIdx, 'data': output }
        print item
        yield json.dumps(item) + '\n'
    expireTokens()
        
@app.route('/getimages')
def getImage():
    '''
    Server address::
 
        /getimages?token=nnn&block=false

    Request parameters::

        token - The number returned by the submitquery service
    
    Response: 
    
        Zero or more JSONItem objects. The 'path' key/value pair
        contains the path that an HTTP request can use to obtain the
        preview PNG. An 'index' key/value indicates the corresponding query
        result row for this image. A 'type' key indicating that this is a
        'preview' is included in case it's useful.
        
        An empty response is _not_ an indication that all previews have been
        generated. It is up to the client to keep track of how many
        previews are expected. Sending requests to this service more than
        2-4 times a second is probably not useful.
        Previews are generated after a call to the result service.
    
    Data structure::
    
        {'url': '/images/previewJHUqde.png', 'index': 0, 'type': 'preview'}
        {'url': '/images/previewriWX1Z.png', 'index': 1, 'type': 'preview'}
        {'url': '/images/previewf0PvMb.png', 'index': 2, 'type': 'preview'}
        ...
        
    Example:

    >>> import webtest
    >>> import music21.musicNet.musicNetServer as mns
    >>> webapp = webtest.TestApp(mns.app)
    >>> req = { 'nodes': [ { 'type': 'Note', 'name': 'n2' } ], 'startNode': 'n2', 'makePreviews': True }
    >>> r = webapp.post_json('/submitquery', req)
    >>> import json
    >>> token = json.loads(r.body)['token']
    >>> r = webapp.get('/getresults?token=%s&start=0&limit=1' % token)
    >>> r = webapp.get('/getimages?token=%s' % token)                           # doctest: +SKIP
    >>> sorted(json.loads(r.body).items())                                      # doctest: +SKIP
    [(u'index', 0), (u'type', u'preview'), (u'url', u'/images/preview...png')]  # doctest: +SKIP
    '''
    token = int(flask.request.args.get('token', ''))
    return flask.Response(generate_images(token), mimetype='application/json')

def generate_images(token):
    if token not in app.tokens:
        item = { 'error': 'That token is invalid or has expired.' }
        yield json.dumps(item) + '\n'
        raise StopIteration
    makePreviews = app.tokens[token][1]
    if not makePreviews:
        item = { 'error': 'makePreviews was not set in the query submission.' }
        yield json.dumps(item) + '\n'
        raise StopIteration
    while True:
        val = app.redis.lpop('outQueue')
        if val == None:
            break
        imageDict = pickle.loads(val)
        addImage(imageDict)
    images = app.images.pop(token, [])
    while images:
        imageDict = images.pop(0)
        item = { 'type': 'preview', 'index': imageDict['index'], 'url': '/images/' + imageDict['path'] }
        yield json.dumps(item) + '\n'
    expireTokens()
    
def addImage(imageDict):
    imgToken = imageDict['token']
    try:
        app.images[imgToken].append(imageDict)
    except KeyError:
        app.images[imgToken] = [imageDict]

def addComparisonFilter(query, comparison, properties):
    pre = comparison['pre']
    post = comparison['post']
    if comparison['preType'] == 'property':
        pre = properties[pre]
        if pre.name == 'MaxDistanceBetweenNotes':
            relation = pre.parent
            relation.maxDistance = post
            return
    if comparison['postType'] == 'property':
        post = properties[post]
    query.addComparisonFilter(pre, comparison['operator'], post)

def makeScore(row, metadata, columns, previews):
    objects = []
    objects_meta = []
    nonobjects = []
    nonobjects_meta = []
    for i in range(len(row)):
        item = row[i]
        #1.4: superclass = py2neo.rest.Resource
        superclass = py2neo.neo4j.Resource
        if isinstance(item, superclass):
            objects.append(item)
            objects_meta.append(metadata[i])
        elif not item is None:
            nonobjects.append(item)
            nonobjects_meta.append(metadata[i])
    # get extended results and limit to requested values
    print 'Objects: ' + repr(objects)
    q = music21.musicNet.Query(app.db)
    result = q.getResultProperties(objects)
    objects = [objectValueMap(x[1]) for x in result]
    items = objects + nonobjects
    headers = objects_meta + nonobjects_meta
    output = []
    metaOutput = columns[:]
    for r in metaOutput:
        output.append(items[headers.index(r)])
    addScoreInfo(result, output, metaOutput)
    print 'Result: ' + repr(result) ###
    previewData = None
    if previews:
        output_objects = []
        output_meta = []
        for i in range(len(result)):
            if objects_meta[i] in columns:
                output_objects.append(result[i][0])
                output_meta.append(objects_meta[i])
        previewData = { 'result': output_objects, 'metadata': output_meta }
    return output, metaOutput, previewData

def addScoreInfo(results, row, metadata):
    metadata.extend(['score', 'parts', 'measures'])
    instruments = set()
    parts = set()
    mms = set()
    filepath = ''
    for el in results:
        kind = el[1]['type']
        if (kind == 'Instrument'):
            instruments.add(el[1]['partName'])
        elif (kind == 'Part'):
            parts.add(str(el[1]['number']))
        elif (kind == 'Measure'):
            mms.add(el[1]['number'])
        elif (kind == 'Score'):
            filepath = el[1]['corpusFilepath']
    if len(mms) > 1:
        measureTxt = '%d-%d' % (min(mms), max(mms))
    else:
        measureTxt = '%d' % mms.pop()
    if instruments:
        partNames = ','.join(instruments)
    else:
        partNames = ','.join(parts)
    row.extend([filepath, partNames, measureTxt])

def objectValueMap(data):
    kind = data['type']
    if kind == 'Note':
        return data['pitch']
    if kind == 'NoteSimultaneousWithNote':
        return data['harmonicInterval']
    if kind == 'NoteToNote':
        return data['interval']
    else:
        return '-'

def expireTokens():
    # Remove tokens that are older than 30 minutes.
    for token in app.tokens:
        if app.tokens[token][2] - time.time() > 1800:
            del app.tokens[token]


if __name__ == "__main__":
    import optparse
    parser = optparse.OptionParser()
    parser.add_option('-a', '--address', dest='address', default='127.0.0.1',
                      help='-a|--address : the IP address of the server')
    (options, args) = parser.parse_args()
    
    start = time.clock()
    print "Loading relationship types..."
    app.db.listRelationshipTypes()
    print "Loading node property values..."
    app.db.listNodePropertyValues()
    print "Loading relationship property values..."
    app.db.listRelationshipPropertyValues()
    
    print 'Running. (Startup in %d seconds)' % (time.clock() - start)
    app.run(host=options.address) #reloader=True
    
    
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. 
# If a copy of the MPL was not distributed with this file, You can obtain one at 
# http://mozilla.org/MPL/2.0/.

