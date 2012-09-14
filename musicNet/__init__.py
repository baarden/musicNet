#!/usr/bin/python
#-------------------------------------------------------------------------------
# Name:         musicNet.py
# Purpose:      classes for connecting music21 to, and searching, Neo4j
#
# Authors:      Bret Aarden
# Version:      0.1
# Date:         29Jul2012
#
# License:      MPL 2.0
#-------------------------------------------------------------------------------


'''
.. testsetup:: *

    import music21.musicNet
    music21.musicNet._prepDoctests()

The musicNet module provides objects for searching music data in a network database
(specifically the Neo4j database), and for adding music21 data to the database. 
Queries are constructed by creating relationships between objects (notes, measures, parts, etc.).
This hides much of the mechanics of searching through music data, but
the database format is relatively fixed, unlike music21.

The :class:`Database` object provides an interface with the database to add a music21 
:class:`~music21.stream.Score` and get information about the database contents, 
such as the available scores, relationships, and properties.

The :class:`Query` object provides an interface for building and executing queries of the database.
It also has a :meth:`Query.music21Score` method for generating a music21
Score from a result.

There are also database :class:`Entity` objects that help with building the queries, including
:class:`Node`, :class:`Relationship`, :class:`Property`, and :class:`Filter` objects.

:class:`Moment` objects can be added to music21 objects using the module-level 
:meth:`addMomentsToScore` method. These objects have a similar function to
:class:`~music21.voiceLeading.VerticalSlice` objects. When present in a score, they
add cross-:class:`~music21.stream.Part` relationships to Notes in the database.

Configuration
-------------

This module requires that the `py2neo <http://py2neo.org/>`_ Python module be installed,
and that you have access to a `Neo4j <http://neo4j.org/>`_ database (version 1.4 or newer).

The database must be configured to use automatic indexing. (Well, technically it doesn't, 
but it will make open-ended searches a lot faster.) This involves editing the
`conf/neo4j.properties` file in the database folder. The following lines should be 
uncommented (the leading hash mark should be deleted) and edited as shown::

    node_auto_indexing=true
    node_keys_indexable=type
    relationship_auto_indexing=true
    relationship_keys_indexable=type

Neo4j operation is nearly plug-and-play. To start the database server from the command line,
just move to the database directory and enter::

    $ bin/neo4j start    
'''

#TO DO:
#LONG TERM:
#Add RESTful interface using Bottle.
#Implement a programmatic replacement for Query.addCypherFilter(),
#  maybe by overriding Python operators, a la SQLAlchemy?
#Add inspect disk caching.
#Optimize speed and memory profiles. Goldberg Variations, argggh!

#Suggestions for music21:
#Change Measure.keySignature, .timeSignature, and .clef from None to weakRefs.

#Suggestions for py2neo
#Authorization on databases doesn't appear to work with .create().
#Tornado __del__ errors on exit should be fixed!


import sys
import time
import random
import weakref
import unittest, doctest
import py2neo.neo4j
import music21
from music21 import *


def _prepDoctests():
    '''Execute this function before running doctests. 
    Results from the tests depend on the state of the database, and there is
    no way (!?!) to control the order of block testing. This function will
    purge the database and import a sample file.
    '''
    db = Database()
    db.wipeDatabase()
    bwv84_5 = corpus.parse('bach/bwv84.5.mxl')
    addMomentsToScore(bwv84_5)
    db.addScore(bwv84_5, index='bach/bwv84.5.mxl')

def addMomentsToScore(score):
    '''Adds :class:`Moment` objects to a :class:`~music21.stream.Score`.
    Without them, the :meth:`Database.addScore` method will not 
    be able to add vertical note relationships to the database, such as
    `NoteSimultaneousWithNote`, `NoteStartsAtMoment`, and `NoteSustainedAtMoment`.
    
    >>> bwv84_5 = corpus.parse('bach/bwv84.5.mxl')
    >>> print len(bwv84_5)
    6
    >>> from music21.musicNet import *
    >>> addMomentsToScore(bwv84_5)
    >>> print len(bwv84_5)
    73
    '''
    flatscore = score.flat
    alloffsets = list(set([x.offset for x in flatscore.notesAndRests]))
    alloffsets.sort()
    for offset in alloffsets:
        notes = flatscore.getElementsByOffset(offset, mustBeginInSpan=False,
                                              classList=[note.Note])
        notes = [x for x in notes]
        moment = Moment(notes)
        score.insert(offset, moment)

def _signedModulo(val, mod):
    ''' This modulo function will return both negative and positive numbers.
    '''
    if val == 0:
        return 0
    sign = val / abs(val)
    while abs(val) > mod:
        val -= mod * sign
    return val

def _convertFromString(val):
    if not isinstance(val, (str, unicode)):
        return val
    if val == 'None':
        val = None
    elif val == 'True':
        val = True
    elif val == 'False':
        val = False
    else:
        if val.find('.') >= 0:
            try:
                val = float(val)
            except ValueError: pass
        else:
            try:
                val = int(val)
            except ValueError: pass
    return val

#-------------------------------------------------------------------------------
class Database(object):
    '''An object that connects to a Neo4j database, imports music21 scores,
    and provides information about the contents of the database.
    
    By default it assumes the Neo4j database is available at the standard
    location on the current machine (`http://localhost:7474/db/data/`), but the
    `uri` argument can be used to specify a remote or non-standard location.
    (Note that the URI should end with a slash!) Any keyword arguments are
    passed on to a :class:`py2neo.neo4j.GraphDatabaseService` object.
    
    >>> db = Database()
    >>> print db.graph_db
    GraphDatabaseService(http://localhost:7474/db/data/)
    '''
    
    _DOC_ORDER = [ 'wipeDatabase', 'addScore', 'listScores', 'listNodeTypes', 'listNodeProperties', 
                   'listRelationshipTypes', 'listRelationshipProperties', 'printStructure',
                   'addPropertyCallback' ]
    _DOC_ATTR = {
    'graph_db': 'The instance of a :class:`py2neo.neo4j.GraphDatabaseService` object connected to this object, which is in turn connected to a Neo4j server either at the default location on the present computer, or the one specified by the Database `uri` argument.',             
    }
    
    def __init__(self, uri='http://localhost:7474/db/data/', **kwargs):
        self.graph_db = py2neo.neo4j.GraphDatabaseService(uri, **kwargs)
        self._db_kwargs = kwargs
        self._db_uri = uri
        self._callbacks = {}
        self._extractState = {}
        self._defaultCallbacks()
        self._m21SuperclassLookup = self._inspectMusic21ExpressionsArticulations()
        self._skipProperties = ('nodeRef', 'parent', '_activeSite', 'id', '_classes', 'groups',
                               '_derivation', '_overriddenLily', '_definedContexts', '_activeSiteId', 
                               '_idLastDeepCopyOf', '_mutable', '_elements', '_cache', 'isFlat', 
                               'autosort', '_components', '_unlinkedDuration', 'isSorted', 
                               'flattenedRepresentationOf', '_reprHead', 'idLocal', 'autoSort',
                               'inherited') 

    def wipeDatabase(self):
        '''Removes all relationships and nodes from the database.

        >>> db = Database()
        >>> db.wipeDatabase()
        >>> print db.graph_db.get_relationship_count()
        0
        >>> print db.graph_db.get_node_count()
        1
        >>> bwv84_5 = corpus.parse('bach/bwv84.5.mxl')     #_DOCS_HIDE
        >>> addMomentsToScore(bwv84_5)                     #_DOCS_HIDE
        >>> db.addScore(bwv84_5, index='bach/bwv84.5.mxl') #_DOCS_HIDE

        The node count can never go below 1 because Neo4j always keeps a reference node 
        in its network graph.
        '''
        q = Query(self)
        q.setStartRelationship()
        q.setLimit(100)
        while (self.graph_db.get_relationship_count()):
            results, meta = q.execute()
            results = [x[0] for x in results]
            self.graph_db.delete(*results)
        q = Query(self)
        q.setStartNode()
        q.setLimit(100)
        while (self.graph_db.get_node_count() > 1):
            results, meta = q.execute()
            results = [x[0] for x in results]
            self.graph_db.delete(*results)

    def addScore(self, score, index=None, verbose=False):
        '''Adds a music21 :class:`~music21.stream.Score` to the database.
        In case the score does not contain :class:`~music21.metadata.Metadata` information
        about the name of the score, or if we want a standardized name for each score,
        we can add an `index` argument, which will become a property on the score node. 
        To see progress on the import, we can set the `verbose` argument to `True`.
        
        In order to be able to access vertical note relationships such as
        `NoteSimultaneousWithNote`, `NoteStartsAtMoment`, and `NoteSustainedAtMoment`,
        we need to add :class:`Moment` objects to the score using the 
        module-level :meth:`addMomentsToScore` method before calling this method.
        
        >>> db = Database()
        >>> db.wipeDatabase() #_DOCS_HIDE
        >>> bwv84_5 = corpus.parse('bach/bwv84.5.mxl')
        >>> addMomentsToScore(bwv84_5)
        >>> db.addScore(bwv84_5, index='bach/bwv84.5.mxl')
        >>> print db.graph_db.get_node_count()
        459
        >>> print db.graph_db.get_relationship_count()
        1519
        '''        
        self.nodes = []
        self.edges = []
        self.nodeLookup = {}
        self._extractState = { 'verbose': verbose,
                              'nodeCnt': 0,
                              'relationCnt': 0 }
        import copy
        score = copy.deepcopy(score)
        if index:
            score.index = index
        if verbose:
            self.lastProgress = 0
            self._timeUpdate(report=False)
            self._extractState['partItemMax'] = sum([len(x) for x in score.parts])
            sys.stderr.write('Extracting music21 objects..........')
        self._extractNodes(score)        
        self._writeNodesToDatabase()        
        self._writeEdgesToDatabase()

    def listScores(self):
        '''Returns a list of dict objects with information about the scores that have been added 
        to the database, with keys for `movementName`, `_names` (a list of 
        contributor/composer names), and `index`.

        >>> db = Database()
        >>> expectedScores = [{u'index': u'bach/bwv84.5.mxl', u'_names': [None], u'movementName': u'bwv84.5.mxl'}]
        >>> db.listScores() == expectedScores
        True
        '''
        q = Query(self)
        score = q.setStartNode(nodeType='Score')
        inScore = q.addRelationship(relationType='MetadataInScore', end=score, optional=True)
        meta = inScore.start
        inMetadata = q.addRelationship(relationType='ContributorInMetaData', end=meta, optional=True)
        contributor = inMetadata.start
        q.addReturns(meta.movementName, contributor._names, score.index)
        results, meta = q.execute()
        columns = [x[x.find('.') + 1:] for x in meta]
        scores = {}
        for row in results:
            index = row[0] or row[2]
            if index not in scores:
                scores[index] = {}
            for i in range(len(row)):
                col = columns[i]
                if col not in scores[index]:
                    scores[index][col] = set()
                scores[index][col].add(row[i])
        for score in scores.itervalues():
            for col in score:
                if col == '_names':
                    score[col] = list(score[col])
                else:
                    score[col] = score[col].pop()
        return scores.values()

    def listNodeTypes(self):
        '''Returns a set of node types available in the database.
        
        >>> db = Database()
        >>> nTypes = db.listNodeTypes()
        >>> print 'Instrument' in nTypes
        True
        '''
        if hasattr(self, 'nTypes'):
            return self.nTypes
        self.listRelationshipTypes()
        return self.nTypes

    def listNodeProperties(self):
        '''Returns a list of node properties in the database, represented as tuples
        (node type, property name).
        
        For instance, to see what properties are available in score nodes:
        
        >>> db = Database()
        >>> props = db.listNodeProperties()
        >>> print sorted( [x for x in props if x[0]=='Score'] )
        [(u'Score', u'_atSoundingPitch'), (u'Score', u'_priority'), (u'Score', u'corpusFilepath'), (u'Score', u'hideObjectOnPrint'), (u'Score', u'index'), (u'Score', u'offset')]
        '''
        if hasattr(self, 'nodeProperties'):
            return self.nodeProperties
        if not hasattr(self, 'nTypes'):
            self.listNodeTypes()
        self.nodeProperties = []
        q = Query(self)
        q.setLimit(50)
        for nodeType in self.nTypes:
            q.setStartNode(nodeType=nodeType)
            q.execute()
            properties = set()
            for node in q.getResults():
                for prop in node[0].get_properties():
                    if prop == 'type':
                        continue
                    properties.add(prop)
            for p in properties:
                self.nodeProperties.append((nodeType, p))
        return self.nodeProperties
    
    def listRelationshipTypes(self):
        '''Returns a list of relationship types in the database, represented as 
        dict objects with keys for `start`, `type`, and `end`. By convention,
        starts and ends in relationships read as a right-directed arrow::
        
            Start--Relationship-->End
            
        For instance, to see if any of the scores in the database 
        have `MetadataInScore` relationships (let's hope they do!), we could do this:
        
        >>> db = Database()
        >>> rTypes = db.listRelationshipTypes()
        >>> print [x['type'] for x in rTypes if x['type']=='MetadataInScore']
        [u'MetadataInScore']
        '''
        if hasattr(self, 'rTypes'):
            return self.rTypes
        self.rTypes = []
        self.nTypes = set()
        rTypes = set()
        relateTypes = self.graph_db.get_relationship_types()
        for relateType in relateTypes:
            q = Query(self)
            r = q.setStartRelationship(relationType=relateType)
            q.addReturns(r.start.type, r.end.type)
            q.execute()
            for n1, n2 in q.getResults():
                rTypes.add( (n1, relateType, n2) )
                self.nTypes.add(n1)
                self.nTypes.add(n2)
        for start, r, end  in rTypes:
            self.rTypes.append( { 'start': start, 'type': r, 'end': end } )
        return self.rTypes
    
    def listRelationshipProperties(self):
        '''Returns a list of relationship properties in the database, represented as tuples
         (relationship type,  property name).
        
        For instance, to see what properties are available in `NoteSimultaneousWithNote`
        relationships:
        
        >>> db = Database()
        >>> rProps = db.listRelationshipProperties()
        >>> print sorted( [x for x in rProps if x[0]=='NoteSimultaneousWithNote'] )
        [(u'NoteSimultaneousWithNote', u'harmonicInterval'), (u'NoteSimultaneousWithNote', u'sameOffset'), (u'NoteSimultaneousWithNote', u'simpleHarmonicInterval')]
        '''
        if hasattr(self, 'relateProperties'):
            return self.relateProperties
        rTypes = set()
        for x in self.listRelationshipTypes():
            rTypes.add(x['type']) 
        self.relateProperties = []
        q = Query(self)
        q.setLimit(50)
        for rType in rTypes:
            q.setStartRelationship(relationType=rType)
            q.execute()
            properties = set()
            for relate in q.getResults():
                for prop in relate[0].get_properties():
                    if prop == 'type': continue
                    properties.add(prop)
            for p in properties:
                self.relateProperties.append( (rType, p) )
        return self.relateProperties

    def printStructure(self):
        '''This is a wrapper for three other methods: it prints the output
        from :meth:`listRelationshipTypes`, :meth:`listNodeProperties`,
        :meth:`listRelationshipProperties` in a long but readable list.
        '''
        print '\nThe following relationships are available in the database:'
        rList = []
        rTypes = self.listRelationshipTypes()
        for r in rTypes:
            rList.append('(%s)-[:%s]->(%s)' % (r['start'], r['type'], r['end']))
        rList.sort()
        for relation in rList:
            print relation

        print '\nThe following node properties are available in the database:'
        properties = self.listNodeProperties()
        last = ''
        for nodeType, prop in properties:
            if nodeType != last:
                print nodeType
            print '  .%s' % prop
            last = nodeType

        print '\nThe following relationship properties are available in the database:'
        properties = self.listRelationshipProperties()
        for rType, prop in properties:
            if rType != last:
                print rType
            print '  .%s' % prop
            last = rType

    def addPropertyCallback(self, entity, callback):
        '''**For advanced use only.**
        
        All music21 objects have default handling when being imported. When
        default handling isn't enough, one or more callbacks can be added to
        preprocess the object, or prevent it from being added to the database.
        The `entity` argument is the class name the callback should be added to
        (as a string), and the `callback` argument is a function object to be
        called. Multiple callbacks can be added to the same class, and an object
        can have callbacks for multiple classes (for instance, a trill will have
        callbacks for both 'Trill' and 'Expression'). Built-in callbacks are set 
        in the internal :meth:`_defaultCallbacks` method.
        '''
        if entity not in self._callbacks:
            self._callbacks[entity] = []
        self._callbacks[entity].append(callback)

    def _defaultCallbacks(self):
        HIDEFROMDATABASE = 1
                
        # Contributor
        def addTextToContributor(db, contributor):
            contributor._names = unicode(contributor.name)
        self.addPropertyCallback('Contributor', addTextToContributor)
        
        # Part
        def addPartNumber(db, part):
            score = part.parent
            for i in range(len(score)):
                if score[i] == part:
                    break
            part.vertex['number'] = i
            db._extractState['history'] = { 'NoteToNote': {}, 'NoteToNoteByBeat': {} }
            for key in ('clef', 'timeSignature', 'keySignatureSharps', 'keySignatureMode'):
                db._extractState[key] = None
        self.addPropertyCallback('Part', addPartNumber)
                
        # Measure
        def addSignaturesAndClefs(db, measure):
            if measure.clef:
                self._extractState['clef'] = measure.clef.classes[0]
            measure.vertex['clef'] = self._extractState['clef']
            if measure.timeSignature:
                self._extractState['timeSignature'] = unicode(measure.timeSignature)
            measure.vertex['timeSignature'] = self._extractState['timeSignature']
            if measure.keySignature:
                self._extractState['keySignatureSharps'] = measure.keySignature.sharps
                self._extractState['keySignatureMode'] = measure.keySignature.mode
            measure.vertex['keySignatureSharps'] = self._extractState['keySignatureSharps']
            measure.vertex['keySignatureMode'] = self._extractState['keySignatureMode']
        self.addPropertyCallback('Measure', addSignaturesAndClefs)            
        
        # Chord
        def labelChordNotes(db, chordObj):
            chordObj.sortAscending(inPlace=True)
            for i in range(len(chordObj)):
                noteObj = chordObj[i]
                noteObj.voice = len(chordObj) - i
                noteObj.parent = chordObj.parent
            return HIDEFROMDATABASE
        self.addPropertyCallback('Chord', labelChordNotes)
        
        # Voice
        def labelVoiceNotes(db, voice):
            for noteObj in voice:
                noteObj.voice = int(voice.id)
                noteObj.parent = voice.parent
            return HIDEFROMDATABASE
        self.addPropertyCallback('Voice', labelVoiceNotes)

        # Note
        def addNoteVoiceleading(db, noteObj):
            def addVoiceleading(db, relationship, noteObj, offset):
                if noteObj.isRest:
                    return
                history = db._extractState['history']
                try:
                    prevNote, prevOffset = history[relationship][noteObj.voice]
                    voiceleadingHistory = True
                except KeyError:
                    voiceleadingHistory = False
                # Only calculate voiceleading for notes within the span of a measure.
                if voiceleadingHistory and offset - prevOffset <= noteObj.parent.barDuration:
                    mint = noteObj.midi - prevNote.midi
                    db._addEdge(prevNote, relationship, noteObj, { 'interval': mint } )
                history[relationship][noteObj.voice] = [noteObj, offset]
            
            offset = noteObj.parent.offset + noteObj.offset
            if not hasattr(noteObj, 'voice'):
                noteObj.voice = 1
            addVoiceleading(db, 'NoteToNote', noteObj, offset)
            if noteObj.offset % 1 == 0:
                addVoiceleading(db, 'NoteToNoteByBeat', noteObj, offset)
        self.addPropertyCallback('Note', addNoteVoiceleading)

        # Pitch
        def addPitchToNote(db, pitchObj):
            noteObj = pitchObj.parent
            noteObj.vertex['pitch'] = pitchObj.nameWithOctave
            noteObj.vertex['midi'] = pitchObj.midi
            noteObj.vertex['microtone'] = pitchObj.microtone.cents
            return HIDEFROMDATABASE
        self.addPropertyCallback('Pitch', addPitchToNote)

        # Duration
        def addDurationToParent(db, durationObj):
            parentType = durationObj.parent.__class__.__name__
            if parentType not in ('StaffGroup', 'Instrument', 'Metadata'):
                durationObj.parent.vertex['quarterLength'] = durationObj.quarterLength
            if parentType == 'Note':
                durationObj.parent.vertex['isGrace'] = durationObj.isGrace
                if hasattr(durationObj, 'stealTimePrevious'):
                    for attr in ('stealTimePrevious', 'stealTimeFollowing', 'slash'):
                        durationObj.parent.vertex[attr] = getattr(durationObj, attr)
            return HIDEFROMDATABASE
        self.addPropertyCallback('Duration', addDurationToParent)
        
        # MetronomeMark
        def simplifyText(db, mm):
            mm._tempoText = unicode(mm._tempoText)
        self.addPropertyCallback('MetronomeMark', simplifyText)
        
        # Expression, Articulation
        def useAbstractType(db, obj):
            abstractions = ('Expression', 'Articulation')
            superclass = [x for x in obj.classes if x in abstractions][0]
            obj.vertex['type'] = superclass
            obj.vertex['name'] = obj.__class__.__name__
        self.addPropertyCallback('Expression', useAbstractType)
        self.addPropertyCallback('Articulation', useAbstractType)

        # Trill, Mordent, Turn, Schleifer
        def simplifyOrnamentInterval(db, ornament):
            if not hasattr(ornament, 'size'):
                return
            if not isinstance(ornament.size, (str, unicode)):
                ornament.size = ornament.size.directedName
        self.addPropertyCallback('Trill', simplifyOrnamentInterval)
        self.addPropertyCallback('GeneralMordent', simplifyOrnamentInterval)
        self.addPropertyCallback('Turn', simplifyOrnamentInterval)
        self.addPropertyCallback('Schleifer', simplifyOrnamentInterval)

        # Beams
        def addBeams(db, beams):
            noteObj = beams.parent
            for beam in beams.beamsList:
                beam.parent = noteObj
                db._addNode(beam)
            return HIDEFROMDATABASE
        self.addPropertyCallback('Beams', addBeams)
        
        # Clef
        def addMidmeasureClefs(db, clefObj):
            if clefObj.offset == 0:
                return HIDEFROMDATABASE
            clefObj.vertex['type'] = 'MidmeasureClef'
            clefObj.vertex['name'] = clefObj.__class__.__name__
        self.addPropertyCallback('Clef', addMidmeasureClefs)
        
        # Moment
        def addCrossPartRelationships(db, moment):
            simultaneous = list(moment.__dict__.pop('simultaneous'))
            for noteObj in simultaneous:
                db._addEdge(noteObj, 'NoteSustainedAtMoment', moment)
            sameOffset = list(moment.__dict__.pop('sameOffset'))
            for noteObj in sameOffset:
                db._addEdge(noteObj, 'NoteStartsAtMoment', moment)
            notes = sameOffset + simultaneous
            simuls = {}
            for i in range(len(notes) - 1):
                note1 = notes[i]
                for j in range(i + 1, len(notes)):
                    note2 = notes[j]
                    if note1 in simuls:
                        if note2 in simuls[note1]: continue
                    else:
                        simuls[note1] = {}
                    if note2 in simuls and note1 in simuls[note2]:
                        continue
                    cInt = note1.midi - note2.midi
                    sInt = _signedModulo(note1.midi - note2.midi, 12)
                    properties = { 'harmonicInterval': cInt,
                                   'simpleHarmonicInterval': sInt,
                                   'sameOffset': 'False' }
                    if note1.offset == note2.offset:
                        properties['sameOffset'] = 'True'
                    db._addEdge(note1, 'NoteSimultaneousWithNote', note2, properties)
                    simuls[note1][note2] = True
        self.addPropertyCallback('Moment', addCrossPartRelationships)
        
        # Spanner
        def addSpannerRelationship(db, span):
            kind = span.__class__.__name__
#            if kind == 'Moment': return
            if len(span.getComponents()) > 2:
                if kind == 'StaffGroup':
                    for i in range(len(span)):
                        part = span[i]
                        db._addEdge(part, 'PartInStaffGroup', span)
                    return
                elif kind not in ('Slur'):
                    raise TypeError('Handling of "%s" Spanners is not supported.' % kind)
            first = span.getFirst()
            last = span.getLast()
            span.name = kind
            # music21 1.0 handling of gracenote slurs is broken, therefore:
            if (first.measureNumber != None and last.measureNumber != None):
                db._addEdge(first, 'spannerTo', last, span)
            return HIDEFROMDATABASE
        self.addPropertyCallback('Spanner', addSpannerRelationship)

        # Optional objects
        # NoteEditorial
        def skipIfEmpty(db, obj):
            objDict = obj.__dict__
            for key, val in objDict.iteritems():
                if key in self._skipProperties:
                    continue
                if key in ('vertex', 'position'):
                    continue # There are empty objects with non-empty positions.
                if not val:
                    continue
                if hasattr(val, '__dict__'):
                    if skipIfEmpty(db, val):
                        continue
                return
            return HIDEFROMDATABASE
        self.addPropertyCallback('NoteEditorial', skipIfEmpty)
        
        # Omitted objects
        # TimeSignature, KeySignature, MiscTandam
        def skipThisObject(db, obj):
            return HIDEFROMDATABASE
        self.addPropertyCallback('TimeSignature', skipThisObject)
        self.addPropertyCallback('KeySignature', skipThisObject)
        self.addPropertyCallback('MiscTandam', skipThisObject)

    def _inspectMusic21ExpressionsArticulations(self):
        import inspect
        lookup = {}
        # search music21 modules:
        for module in (expressions, articulations):
            sName = module.__name__[8:].capitalize()
            classes = inspect.getmembers(module, inspect.isclass)
            for cName, ref in classes:
                lookup[cName] = sName
        return lookup

    def _timeUpdate(self, report=True):
        newtime = time.time()
        if report:
            sys.stderr.write('(%.1f seconds)\n' % (newtime - self.refTime))
        self.refTime = newtime

    def _progressReport(self, state, minIn, maxIn, minOut, maxOut):
        if not hasattr(self, 'lastProgress'):
            lastOut = self.lastProgress = 0
        lastOut = self.lastProgress
        rangeIn = maxIn - minIn
        rangeOut = maxOut - minOut
        progress = rangeOut * (state - minIn) / rangeIn + minOut
        progress = 5 * round(progress / 5)
        if progress >= lastOut + 5:
            increment = int(30.0 * (5.0 / rangeOut))
            sys.stderr.write('=' * increment)
            self.lastProgress = progress
    
    def _extractNodes(self, obj, parent=None):
        '''
        Put all the hierarchical nodes and their relationships into linear order, 
        and indexes {node: order number} in a dict.
        If changes need to be made to a node, the dict can be used to find it in the order.
        At this point relationships store references to the original music21 objects.
        '''
        if not hasattr(obj, 'parent'):
            obj.parent = parent
        state = self._extractState
        if state['verbose'] and parent.__class__.__name__ == 'Part':
            state['partItemCnt'] = state.get('partItemCnt', 0) + 1
            self._progressReport(state['partItemCnt'], 0, state['partItemMax'], 0, 75)
        self._addNode(obj)
        if hasattr(obj, 'classes') and 'Spanner' in obj.classes:
            return
        try:
            itemList = list(obj)
        except TypeError:
            return
        for item in itemList:
            self._extractNodes(item, obj)

    def _addNode(self, node):
        kind = node.__class__.__name__
        node.vertex = { 'type': kind }
        if hasattr(node, 'offset'):
            node.vertex['offset'] = node.offset
        if self._runCallbacks(node) != None:
            return
        self.nodes.append(node)
        idx = len(self.nodes) - 1
        self.nodeLookup[node] = idx
        self._extractObject(node)
        if hasattr(node, 'parent') and node.parent:
            relation = node.vertex['type'] + 'In' + node.parent.vertex['type']
            self._addEdge(node, relation, node.parent)

    def _addEdge(self, start, relationship, end, propertyNode=None):
        if isinstance(propertyNode, dict):
            properties = propertyNode
        elif isinstance(propertyNode, base.Music21Object):
            propertyNode.vertex = {}
            self._extractObject(propertyNode)
            properties = propertyNode.vertex
        else:
            properties = {}
        properties['type'] = relationship
        edge = [ start, relationship, end, properties ]
        self.edges.append(edge)
    
    def _editNode(self, item, key, value):
        idx = self.nodeLookup[item]
        node = self.nodes[idx]
        if value == None:
            value = 'None'
        node.vertex[key] = value
        
    def _runCallbacks(self, node):
        keys = self._callbacks.keys()
        if hasattr(node, 'classes'):
            kinds = [x for x in node.classes if x in keys]
        else:
            name = node.__class__.__name__
            try: 
                kinds = [name, self._m21SuperclassLookup[name]]
            except KeyError:
                kinds = [name]
        for kind in kinds:
            if kind not in self._callbacks:
                continue
            for callback in self._callbacks[kind]:
                rc = callback(self, node)
                if rc != None:
                    return rc

    def _extractObject(self, obj):
        objectDict = obj.__dict__
        for key, val in objectDict.iteritems():
            if key in self._skipProperties:
                continue
            if val == None and key in ('_duration'):
                continue
            elif isinstance(val, list):
                for item in val:
                    if hasattr(item, '__dict__'):
                        self._extractNodes(item, obj)
                continue
            elif isinstance(val, dict):
                for key, text in val.iteritems():
                    obj.vertex[key] = text
                continue
            elif isinstance(val, musicxml.base.MusicXMLElement):
                continue
            elif hasattr(val, '__dict__'): 
                self._extractNodes(val, obj)
                continue
            if key in ('type', 'vertex', 'parent'):
                key = 'm21_' + key
            obj.vertex[key] = val
        for key, val in obj.vertex.items():
            if not isinstance(val, (int, float, long)):
                val = unicode(val)
            obj.vertex[key] = val
    
    def _writeNodesToDatabase(self):
        '''
        When nodes are written to the database in order, 
        references to their database entries will be returned in the same order.
        Those references are saved as attributes in the original nodes.
        '''
        batchSize = 100
        verbose = self._extractState['verbose']
        if verbose:
            self._timeUpdate()
            sys.stderr.write('Writing nodes to database...........')
            maxNodes = len(self.nodes)
            cnt = 0
        while self.nodes:
            subset = self.nodes[:batchSize]
            del self.nodes[:batchSize]
            batchLen = len(subset)
            vertices = [x.vertex for x in subset]
            results = self.graph_db.create(*vertices)
            # Add a .nodeRef attribute to each music21 object 
            # with the address of its corresponding Neo4j node.
            for i in range(batchLen):
                subset[i].nodeRef = results[i]
            self._extractState['nodeCnt'] += batchLen
            if verbose:
                cnt += batchLen
                self._progressReport(cnt, 0, maxNodes, 75, 85)
        
    def _writeEdgesToDatabase(self):
        '''
        Before relationships are written to the database, music21 object references are converted 
        to their corresponding database nodes.
        '''
        batchSize = 100
        verbose = self._extractState['verbose']
        edgeRefs = []
        for edge in self.edges:
            ref1 = edge[0].nodeRef
            ref2 = edge[2].nodeRef
            edgeRef = [ ref1, edge[1], ref2 ]
            # Add a property dictionary, if present.
            if len(edge) > 3:
                edgeRef.append(edge[3])
            edgeRefs.append(edgeRef)
        if verbose:
            self._timeUpdate()
            sys.stderr.write('Writing relationships to database...')
            maxEdges = len(edgeRefs)
            cnt = 0
        while edgeRefs:
            subset = edgeRefs[:batchSize]
            del edgeRefs[:batchSize]
            batchLen = len(subset)
            self.graph_db.create(*subset)
            self._extractState['relationCnt'] += batchLen
            if verbose:
                cnt = cnt + batchLen
                self._progressReport(cnt, 0, maxEdges, 85, 100)
        if verbose:
            self._timeUpdate()

#-------------------------------------------------------------------------------
class Query(object):
    '''This object provides an interface for building and executing queries of the
    database. The first argument to the object must be an existing
    :class:`Database` object.
    
    All queries begin with a starting point, either a node or a relationship.
    This point is set with either the :meth:`setStartNode` or
    :meth:`setStartRelationship` method (but not both).  These nodes and
    relationships are represented by :class:`Node` and :class:`Relationship`
    objects, which are usually created automatically. Note that a starting point
    alone is a complete query structure!

    To get the results and metadata for a query, we use the :meth:`execute`, 
    :meth:`getResults`, or :meth:`getMetadata` methods.
    
    Optionally, we can add a number of restrictions to the search. The
    :meth:`addRelationship` method allows us to connect our start node/relationship to
    other nodes. Any number of nodes can be connected using relationships.
    Each node and relationship in the database has properties, and we use them to filter
    our search using the :meth:`addComparisonFilter` method. 
    
    By default, results are returned as references to entities in the database,
    which can be used by the :meth:`music21Score` method to return a music21
    score fragment for any result row. If we want specific data from the search,
    we can pass those database entities to the :meth:`getResultProperties`
    method to get all of their properties, we can use the :meth:`addReturns`
    method to specify the particular properties we're interested in.
    
    Results are returned in no apparent order, but we can force the database to return
    them sorted according to specific properties using the :meth:`setOrder` method. And
    if we want to limit the number of results, we can use the :meth:`setLimit` method.
    
    To understand what's going on behind the scenes, 
    it may be helpful to read the documentation on the Neo4j 
    `Cypher query language <http://docs.neo4j.org/chunked/stable/cypher-query-lang.html>`_.
    '''
    
    _DOC_ORDER = [ 'setStartNode', 'execute', 'getResults', 'getMetadata', 'getResultProperties', 
                   'setStartRelationship', 'addRelationship', 'addComparisonFilter', 'addCypherFilter', 
                   'addReturns', 'setOrder', 'setLimit',  'music21Score', 'setObjectCallback' ]
    _DOC_ATTR = {
    'db': 'Blah',
    'results': 'Blah',
    'metadata': 'Blah',
    'pattern': 'Blah',
    'nodes': 'Blah'
    }
    
    def __init__(self, db):
        self.db = db
        self._constructCallbacks = {}
        self.results = self.metadata = self.start = None
        self.match = []
        self.where = []
        self.orders = []
        self.returns = []
        self.nodes = set()
        self.limit = ''
        self.phrases = {}
        self._usedNames = []
        self._defaultCallbacks()
        self._inspectMusic21ExpressionsArticulations()
        
    def setStartNode(self, node=None, nodeType=None, name=None, nodeId=None):
        '''Sets a starting :class:`Node` for the Query, and returns that node. 
        Searches begin from this point and branch out to find patterns that fit the query. 
        If we already have a Node object, that can be used as the `node` argument. 
        
        Without a `node` argument a Node is created implicitly, 
        optionally using the `nodeType` argument to specify its type. (Without a type, the
        node will match any type.) The list of available relationship types can be obtained
        from the :meth:`Database.printStructure` method.
        
        A name attribute will be randomly generated unless a `name` argument
        is given. There is no particular reason to name objects unless we want to control 
        the query text exactly, however.
        
        Each node in a Neo4j database has a numeric ID. If a Node with an ID
        number is passed in to the `node` argument, or if a numeric `nodeId`
        argument is given, that ID will be used to target the search.
        
        >>> db = Database()
        >>> q = Query(db)
        >>> print q.setStartNode(nodeType='Metadata', name='Metadata1')
        Metadata1
        
        Calling this method again, or calling :meth:`setStartRelationship`, will
        reset the start point for the Query.
        '''
        if node == None:
            node = Node(self, nodeType=nodeType, name=name)
        if node.id:
            self.start = 'start %s=node(%d)\n' % (node.name, node.id)
        else:
            self.start = 'start %s=node:node_auto_index("type:%s")\n' % (node.name, node.nodeType)
        return node
            
    def execute(self):
        '''
        Executes a query of the database using the current state of the Query object.
        Returns a tuple containing first the results, then the query metadata. 
        See :meth:`getResults` and :meth:`getMetadata` for information on their contents.

        >>> db = Database()
        >>> q = Query(db)
        >>> q.setStartNode(nodeType='Score', name='Score1')
        Score1
        >>> print q.execute()
        ([[Node(http://localhost:7474/db/data/node/...)]], [u'Score1'])
        '''
        import py2neo.cypher as cypher

        self.pattern = self._assemblePattern()
        self.results, meta = cypher.execute(self.db.graph_db, self.pattern, 
                                                     params={'uniqueness': 'none'})
        self.metadata = meta.columns
        return self.results, self.metadata
    
    def getResults(self):
        '''Returns the results of the query as a list of lists. The :meth:`execute` 
        method is called automatically to obtain the results if it has not already 
        been called. Each call to this method will return the same list of results
        until the :meth:`execute` method is called again.
        
        If the :meth:`addReturns` method was used to specify particular 
        properties, then each result is a list of those properties as text strings.
        
        If no return properties have been specified by the :meth:`addReturns`
        method, each result will be a list of :class:`py2neo.neo4j.Node` and
        :class:`py2neo.neo4j.Relationship` objects. In this case, a result list
        can be passed to :meth:`music21Score` to get a music21
        :class:`~music21.stream.Score` fragment containing the result. Or
        passing a result list to the :meth:`getResultProperties` method will add
        all the properties of the objects to the list.
        
        >>> db = Database()
        >>> q = Query(db)
        >>> q.setStartNode(nodeType='Score', name='Score1')
        Score1
        >>> print q.getResults()
        [[Node(http://localhost:7474/db/data/node/...)]]
        '''
        if self.results == None:
            self.execute()
        return self.results

    def getMetadata(self):
        '''Returns the metadata for the query (that is, the column names) as a list. The :meth:`execute` 
        method is called automatically to obtain the results if it has not already 
        been called. Each call to this method will return the same metadata
        until the :meth:`execute` method is called again.
        
        >>> db = Database()
        >>> q = Query(db)
        >>> q.setStartNode(nodeType='Score', name='Score1')
        Score1
        >>> print q.getMetadata()
        [u'Score1']
        '''
        if self.metadata == None:
            self.execute()
        return self.metadata
    
    def getResultProperties(self, result):
        '''Takes a list of :class:`py2neo.neo4j.Node` and
        :class:`py2neo.neo4j.Relationship` objects (the default result format
        if the :meth:`addReturns` method wasn't called), and returns a new list
        in which each object has been replaced by a tuple: the original object, 
        then a dict of that object's database properties. 

        >>> db = Database()
        >>> q = Query(db)
        >>> score = q.setStartNode(nodeType='Score', name='Score1')
        >>> rows = q.getResults()
        >>> nodeInfo = q.getResultProperties(rows[0])[0]
        >>> print sorted(nodeInfo[1].items())
        [(u'_atSoundingPitch', u'unknown'), (u'_priority', 0), (u'corpusFilepath', u'bach/bwv84.5.mxl'), (u'hideObjectOnPrint', False), (u'index', u'bach/bwv84.5.mxl'), (u'offset', 0.0), (u'type', u'Score')]
        '''
        props = self.db.graph_db.get_properties(*result)
        return zip(result, props)

    def setStartRelationship(self, relation=None, relationType=None, start=None, end=None, name=None):
        '''Sets a starting :class:`Relationship` for the Query, and returns that relationship. 
        Searches begin from this point and branch out to find patterns that fit the query.
        
        Without a `relation` argument a Relationship is created implicitly, 
        optionally using the `relationType` argument to specify its type. (Without a type, the
        relationship will match any type.) The list of available relationship types can be
        obtained from the :meth:`Database.printStructure` method.
        
        The `start` and `end` arguments take :class:`Node` objects, and allow the relationship 
        to be connected to other nodes in the query. If either is omitted, a new node object
        is created implicitly to fill it. By convention a relationship is understood to be 
        a right-directed arrow::
        
            Start--Relationship-->End

        A name attribute will be randomly generated unless 
        a `name` argument is given. There is no particular reason to name objects or create them
        explicitly, unless we want to control the query text exactly.

        >>> db = Database()
        >>> q = Query(db)
        >>> score = Node(q, nodeType='Score', name='Score1')
        >>> part = Node(q, 'Part', name='Part1')
        >>> pIS = q.setStartRelationship(relationType='PartInScore', name='PIS1', start=part, end=score)
        >>> print pIS
        (Part1)-[PIS1:PartInScore]->(Score1)
        
        Calling this method again, or calling :meth:`setStartNode`, will
        reset the start point for the Query.
        '''
        if relation == None:
            relation = Relationship(self, relationType, start, end, name)
        if relation not in self.match and relationType is not None:
            self.addRelationship(relation)
        self.start = ('start %s=relationship:relationship_auto_index("type:%s")\n' 
                      % (relation.name, relation.relationType))
        return relation
    
    def addRelationship(self, relation=None, relationType=None, start=None, end=None, name=None, optional=False):
        '''Adds a relationship to the query, and returns the corresponding
        :class:`Relationship` object. Any number of relationships can be added to a query.

        Without a Relationship object passed to the `relation` argument, a
        Relationship is created implicitly, optionally using the `relationType`
        argument to specify its type. (Without a type, the relationship will
        match any type.) The list of available relationship types can be
        obtained from the :meth:`Database.printStructure` method.

        The `start` and `end` arguments take :class:`Node` objects, and allow the relationship 
        to be connected to other nodes in the query. If either is omitted, a new node object
        is created implicitly to fill it. By convention a relationship is understood to be 
        a right-directed arrow::
        
            Start--Relationship-->End

        A name attribute will be randomly generated unless 
        a `name` argument is given. There is no particular reason to name objects or create them
        explicitly, unless we want to control the query text exactly.

        Note that in the following example `pIS.start` is synonymous with `part`.

        >>> db = Database()
        >>> q = Query(db)
        >>> part = Node(q, 'Part', name='Part1')
        >>> pIS = q.setStartRelationship(relationType='PartInScore', name='PIS1', start=part)
        >>> measure = Node(q, 'Measure', name='Measure1')
        >>> mIP = q.addRelationship(relationType='MeasureInPart', name='MIP1', start=measure, end=pIS.start)
        >>> print mIP
        (Measure1)-[MIP1:MeasureInPart]->(Part1)                
        
        Setting the `optional` argument to True will cause the query pattern to match even
        if this relationship doesn't exist in a particular instance.
        '''
        if relation == None:
            relation = Relationship(self, relationType, start=start, end=end, name=name, optional=optional)
        self.match.append(relation)
        for node in (relation.start, relation.end):
            if node.nodeType == '*': continue
            self.nodes.add(node)
        return relation
    
    def addComparisonFilter(self, pre, operator, post):
        '''Adds a condition to the query that must be true in order for the query to match, and returns
        the corresponding :class:`Filter` object.
        
        The `pre` and `post` arguments should be either:
        
        * an attribute of a :class:`Node` or :class:`Relationship` (which will return a
          :class:`Property` object), or
        * some text or a number.
         
        The list of available properties can be obtained from the :meth:`Database.printStructure` 
        method. Of course, if any of those Nodes or Relationships aren't objects in the query,
        the query will fail to match. 
        
        The `operator` argument is a text string of the comparison we want to test
        between the two properties. The available comparison operators are
        `=, <>, <, >, <=,` and `>=`.

        >>> db = Database()
        >>> q = Query(db)
        >>> n1 = Node(q, 'Note', name='Note1')
        >>> print q.addComparisonFilter(n1.midi, '<', 43)
        Note1.midi < 43
                
        There is no need for us to build all these relationships between notes, measures, parts,
        and scores, unless we want to return information about them or add filters to them.
        In this case the Note object by itself would be enough 
        (using just the :meth:`setStartNode` method).
        '''
        filt = Filter(self, pre, operator, post)
        if filt not in self.where:
            self.where.append(filt)
        return filt
        
    def addCypherFilter(self, text):
        '''**For advanced use. This method will be probably be deprecated in future versions.**

        Adds a condition written in 
        `Cypher <http://docs.neo4j.org/chunked/milestone/query-where.html>`_ to the query.
        This method is provided for complex queries that require filters more complicated
        than basic comparisons. 
        
        In referring to a entity being used in the query, the Cypher query text must use 
        the name of the entity, which can be obtained from its `name` attribute.
        
        >>> db = Database()
        >>> q = Query(db)
        >>> nSWN = q.setStartRelationship(relationType='NoteSimultaneousWithNote')
        >>> q.addCypherFilter('abs(%s.midi- %s.midi) %% 12 = 7' % (nSWN.start.name, nSWN.end.name))
        >>> print len(q.getResults())
        70
        
        But we can create this filter more directly using the `simpleHarmonicInterval` property
        of `NoteSimultaneousWithNote` relationships::
        
            q.addComparisonFilter(nSWN.simpleHarmonicInterval, '=', 7)        
        '''
        self.where.append(text)

    def addReturns(self, *props):
        '''Adds a list of properties to be returned from the query. Each of these properties
        should be a :class:`Property` object returned by accessing an attribute of a
        :class:`Node` or :class:`Relationship` object. The list of available attributes can be
        obtained using the :meth:`Database.printStructure` method.
        
        If this method is set, the requested properties will be returned as text, numbers, 
        and so forth. If it is not set, the query will return a list of 
        :class:`py2neo.neo4j.Node` and :class:`py2neo.neo4j.Relationship` objects.
        
        This method can be called multiple times, and the items from each call will be appended
        to the list of returns.
        
        >>> db = Database()
        >>> q = Query(db)
        >>> nSWN = q.setStartRelationship(relationType='NoteSimultaneousWithNote')
        >>> q.addComparisonFilter(nSWN.simpleHarmonicInterval, '=', 7)
        >>> q.addReturns(nSWN.start.midi, nSWN.end.midi)   
        >>> print sorted(q.getResults())[0]

        '''
        for p in props:
            self.returns.append(p)
                
    def setLimit(self, limit):
        '''Sets the maximum number of results that can be returned from the query.
        By default, the database will search for all matches to the query.
        '''
        self.limit = limit
        
    def setOrder(self, propertyObject):
        '''Specifies that the results should be sorted according to the :class:`Property`
        given as the argument, and then returned in that order. Property objects can be obtained 
        by accessing the attributes of :class:`Node` and :class:`Relationship` objects.
        The list of available properties can be obtained from the :meth:`Database.printStructure` 
        method.
        '''
        self.orders.append(propertyObject)
        
    def printResults(self):
        '''Prints out the results of a query in tab-delimited format with the column names in
        the first row. If the :meth:`execute` method has not been called, it is called
        automatically.
        '''
        if self.results == None:
            self.execute()
        sys.stderr.write('%d results:\n' % len(self.results))
        print '\t'.join(self.metadata)
        for row in self.results:
            print '\t'.join([str(x) for x in row])

    def music21Score(self, resultList):
        '''
        Returns a music21 :class:`~music21.stream.Score` object, given a single
        query result (which is by default a list of :class:`py2neo.neo4j.Node`
        and :class:`py2neo.neo4j.Relationship` objects). This method will only
        work if the :meth:`addReturns` method has not been called.
        
        The Score will contain its Metadata, all the measures and parts containing the query results,
        and all the objects contained in those measures, but no more than that.  
        '''
        self.nodeLookup = {}
        result = resultList[:]
        self._addHierarchicalNodes(result)
        result = self.getResultProperties(result)
        nodes = {}
        relations = []
        for itemTuple in result:
            if isinstance(itemTuple[0], py2neo.neo4j.Node):
                if itemTuple[0].id not in nodes:
                    nodes[itemTuple[0].id] = itemTuple
            else:
                relations.append(itemTuple)
        score = stream.Score()
        scoreNodeId = [x for x in nodes if nodes[1]['type'] == 'Score'][0]
        self._addHierarchicalMusic21Data(score, scoreNodeId, nodes, relations)
        return score

    def setObjectCallback(self, entity, callback):
        '''**For advanced use.**

        All node types have default handling when being exported to music21 objects.
        When default handling isn't enough, one or more callbacks can be added
        to preprocess the object, or prevent it from being added to the Score.
        The callback is responsible for adding the object to the score.
        The `entity` argument is the class name the callback should be added to 
        (as a string), and the `callback` argument is a function object to be called.
        Only one callback is allowed per class name, so each call to this method
        will replace the previous value set by the method for that entity.
        Built-in callbacks are set in the internal :meth:`_defaultCallbacks` method.
        '''
        self._constructCallbacks[entity] = callback
    
    def _defaultCallbacks(self):
        
        # TieInNote
        def setTie(self, tieDict, tie, note, r):
            note.tie = tie
            return tie
        self.setObjectCallback('TieInNote', setTie)
        
        # BeamInNote
        def addBeam(self, beamDict, beam, note, r):
            number = _convertFromString(beamDict.get('number', 'None'))
            direction = _convertFromString(beamDict.get('direction', 'None'))
            beamType = beamDict['m21_type']
            if number != None:
                note.beams.setByNumber(number, beamType, direction)
            else:
                note.beams.append(beamType, direction)
            return None
        self.setObjectCallback('BeamInNote', addBeam)
        
        # ContributorInMetadata
        def addContributorText(self, contributorDict, contributor, metadataObj, r):
            name = metadataObj.Text(contributorDict.pop('_names'))
            contributor._names.append(name)
            metadataObj.addContributor(contributor)
            return contributor
        self.setObjectCallback('ContributorInMetadata', addContributorText)
        
        # PartInScore
        def removePartNumber(self, partDict, part, score, r):
            del partDict['number']
            score.insert(part)
            return part
        self.setObjectCallback('PartInScore', removePartNumber)
        
        # MeasureInPart
        def addSignaturesAndClefs(self, measureDict, measure, part, r):
            firstMeasure = (_convertFromString(measureDict['offset']) == self.scoreOffset)
            if measureDict['clefIsNew'] == 'True' or firstMeasure:
                classLookup = self._listMusic21Classes()
                clef = classLookup[measureDict['clef']]()
                measure.insert(clef)
            if measureDict['keyIsNew'] == 'True' or firstMeasure:
                sharps = _convertFromString(measureDict['keySignatureSharps'])
                keySig = key.KeySignature(sharps)
                keySig.mode = _convertFromString(measureDict['keySignatureMode'])
                measure.insert(keySig)
            if measureDict['timeSignatureIsNew'] == 'True' or firstMeasure:
                timeSig = meter.TimeSignature(measureDict['timeSignature'])
                measure.insert(timeSig)
            for key in ('clef', 'keySignatureSharps', 'keySignatureMode', 'timeSignature'):
                del measureDict[key]
            part.insert(measure)
            return measure
        self.setObjectCallback('MeasureInPart', addSignaturesAndClefs)
        
        # MidmeasureClefInMeasure
        def addMidmeasureClef(self, clefDict, clef, measure, r):
            classLookup = self._listMusic21Classes()
            clef = classLookup[clefDict['name']]()
            measure.insert(clef)
            return clef
        self.setObjectCallback('MidmeasureClefInMeasure', addMidmeasureClef)
        
        # NoteInMeasure
        def setPitchAndDuration(self, noteDict, note, measure, r):
            # .nameWithOctave is not read/write in music21 1.0.
            import re
            name = re.sub('[0-9]*', '', noteDict['pitch'])
            note.midi = int(noteDict['midi'])
            note.name = name
            note.duration.isGrace = _convertFromString(noteDict['isGrace'])
            if note.duration.isGrace:
                for attr in ('stealTimePrevious', 'stealTimeFollowing', 'slash'):
                    setattr(note.duration, attr, noteDict[attr])
            del noteDict['pitch']
            del noteDict['midi']
            del noteDict['isGrace']
            measure.insert(note)
            return note
        self.setObjectCallback('NoteInMeasure', setPitchAndDuration)
            
        # ArticulationInNote, ExpressionInNote
        def replaceWithSpecificClass(self, classDict, classObj, noteObj, r):
            classLookup = self._listMusic21Classes()
            childType = classDict['name']
            obj = classLookup[childType]()
            classAttribute = obj.__module__[8:] 
            getattr(noteObj, classAttribute).append(obj)
            return obj
        self.setObjectCallback('ArticulationInNote', replaceWithSpecificClass)
        self.setObjectCallback('ExpressionInNote', replaceWithSpecificClass)
        
        # PartInStaffGroup
        def addPartsToStaffGroup(self, partDict, part, staffGroup, r):
            childId = r[0].start_node.id            
            part = self.nodeLookup.get(childId, None)
            if part:
                staffGroup.addComponents(part)
            return None
        self.setObjectCallback('PartInStaffGroup', addPartsToStaffGroup)
        
        # spannerTo
        def replaceWithSpecificSpanner(self, noteDict, note, otherNote, r):
            classLookup = self._listMusic21Classes()
            spanDict = r[1]
            spanType = spanDict.pop('name')
            span = classLookup[spanType]()
            start = self.nodeLookup[r[0].start_node.id]
            end = self.nodeLookup[r[0].end_node.id]
            span.addComponents(start, end)
            self._addProperties(span, spanDict)
            measure = start.getContextByClass('Measure')
            measure.append(span)
            return None
        self.setObjectCallback('spannerTo', replaceWithSpecificSpanner)
        
        # Default
        def defaultChild(self, childDict, child, parent, r):
            if hasattr(child, 'offset') and hasattr(parent, 'insert'):
                parent.insert(child)
            elif hasattr(parent, 'append'):
                parent.append(child)
            else:
                sys.stderr.write('%s has no append function for %s objects! Please set a callback function.' 
                                 % (parent, child))
                sys.exit(1)
            return child
        self.setObjectCallback('default', defaultChild)

    def _listMusic21Classes(self):
        if hasattr(self, 'classLookup'):
            return self.classLookup
        import pkgutil
        import inspect
        import music21
        self.classLookup = {}
        for importer, modname, ispkg in pkgutil.iter_modules(music21.__path__):
            if ispkg: continue
            mod = sys.modules['music21.' + modname]
            for c in inspect.getmembers(mod, inspect.isclass):
                self.classLookup[c[0]] = getattr(mod, c[0])
        return self.classLookup

    def _inspectMusic21ExpressionsArticulations(self):
        import inspect
        self.m21_classes = {}
        # inspect music21 modules:
        for module in (expressions, articulations):
            mName = module.__name__
            self.m21_classes[mName] = {}
            classes = inspect.getmembers(module, inspect.isclass)
            for cName, ref in classes:
                self.m21_classes[mName][cName] = ref

    def _assemblePattern(self):
        for node in self.nodes:
            self.addComparisonFilter(node.type, '=', node.nodeType)
        startStr = self.start
        if startStr == None:
            sys.stderr.write('setStartNode() or setStartRelationship() must be called first.\n')
            sys.exit(1)
        matchStr = whereStr = returnStr = orderStr = limitStr = ''
        if self.match:
            matchStr = 'match\n' + ',\n'.join([str(x) for x in self.match]) + '\n'
        if self.where:
            whereStr = 'where\n' + '\nand '.join([str(x) for x in self.where]) + '\n'
        if self.returns:
            returnStr = 'return ' + ', '.join([str(x) for x in self.returns]) + '\n'
        else:
            returnStr = 'return *\n'
        if self.orders:
            orderStr = 'order by' + ', '.join([str(x) for x in self.orders]) + '\n'
        if self.limit:
            limitStr = 'limit %d\n' % self.limit
        pattern = startStr + matchStr + whereStr + returnStr + orderStr + limitStr
        return pattern

    def _addHierarchicalNodes(self, results):
        ''' Fill in a minimal score hierarchy sufficient to contain the notes in the result.
        Then fill in all the other notes in the minimal score.
        Then add one more layer of nodes within the objects in the score 
        (articulations, dynamics, expressions, etc.).
        '''
        nodes = {}
        relations = {}
        self._filterNodesAndRelationships(results, nodes, relations)
        
        # For each note in the result, fill in the structural nodes above it.
        q = Query(self.db)
        n = Node(q, 'Note')
        inMeasure = q.addRelationship('NoteInMeasure', start=n)
        inPart = q.addRelationship('MeasureInPart', start=inMeasure.end)
        inScore = q.addRelationship('PartInScore', start=inPart.end)
        q.addRelationship('InstrumentInPart', end=inPart.end, optional=True)
        q.addRelationship('MetadataInScore', end=inScore.end, optional=True)
        q.addRelationship('StaffGroupInScore', end=inScore.end, optional=True)
        for node in results:
            if node._metadata['data']['type'] != 'Note': continue
            node.inQuery = True
            n.id = node.id
            q.setStartNode(n)
            q.execute()
            subresults = q.getResults()
            self._filterNodesAndRelationships(subresults[0], nodes, relations)
        
        # Fill in the other notes in the measures.
        measures = [x for x in nodes.values() if x._metadata['data']['type'] == 'Measure']
        self.scoreOffset = sorted([float(x._metadata['data']['offset']) for x in measures])[0]
        for m in measures:
            self._addChildren(m, 'NoteInMeasure', nodes, relations)

        # Add one more layer of objects below the existing ones.
        rTypes = self.db.listRelationshipTypes()
        for node in nodes.values():
            nType = node._metadata['data']['type']
            inNodeRTypes = [x for x in rTypes if x['end'] == nType]
            for r in inNodeRTypes:
                rType = r['type']
                if not (rType.endswith('In' + nType) or rType == 'spannerTo'):
                    continue
                if rType in ('NoteInMeasure', 'MomentInScore', 'PartInScore', 'MeasureInPart'):
                    continue
                self._addChildren(node, rType, nodes, relations)
        results.extend(nodes.values())
        results.extend(relations.values())

    def _filterNodesAndRelationships(self, results, nodes, relations):
        ''' Nodes and Relations must be hashed separately to avoid ID number clashes.
        '''
        for item in results:
            if isinstance(item, py2neo.neo4j.Node):
                if item.id not in nodes:
                    nodes[item.id] = item
            else:
                relations[item.id] = item
    
    def _addChildren(self, node, rType, nodes, relations):
        ''' Add all the children of this node that are connected by the specified Relationship type.
        '''
        q = Query(self.db)
        n = Node(q, nodeId=node.id)
        q.setStartNode(n)
        q.addRelationship(rType, end=n)
        subresults, meta = q.execute()
        for result in subresults:
            self._filterNodesAndRelationships(result, nodes, relations)
                    
    def _addHierarchicalMusic21Data(self, parent, parentId, nodes, relates):
        # Some bits of the music21-to-MusicXML conversion process are sensitive to order.
        relatesToNode = sorted([x for x in relates if x[0].end_node.id == parentId],
                               key = lambda r: r[0].id)
        for r in relatesToNode:
            rType = r[0]['type']
            parentType = parent.__class__.__name__
            if not (rType.endswith('In' + parentType) or rType in ('spannerTo')):
                continue
            childId = r[0].start_node.id
            childDict = nodes[childId][1]
            child = self._addMusic21Child(childDict, parent, r)
            if child == None:
                continue
            if hasattr(nodes[childId][0], 'inQuery'):
                child.editorial.color = "red"
            self.nodeLookup[childId] = child
            self._addHierarchicalMusic21Data(child, childId, nodes, relates)

    def _addMusic21Child(self, childDict, parent, r):
        classLookup = self._listMusic21Classes()
        childType = childDict['type']
        if childType in classLookup:
            child = classLookup[childType]()
        else:
            child = None
        rType = r[0]['type']
        if rType not in self._constructCallbacks:
            rType = 'default'
        child = self._constructCallbacks[rType](self, childDict, child, parent, r)
        if child == None:
            return None
        self._addMusic21Properties(child, childDict)
        return child

    def _addMusic21Properties(self, obj, objDict):
        for key, val in objDict.iteritems():
            if key in ('type', 'voice'):
                continue
            if key.startswith('m21_'):
                key = key[4:]
            val = _convertFromString(val)
            if hasattr(obj, key):
                setattr(obj, key, val)
            else:
                obj.__dict__[key] = val

#-------------------------------------------------------------------------------
class Entity(object):
    '''The generic class of objects that are used to construct queries.
    All entities require a :class:`Query` object as their first argument
    to establish the namespace which they belong to. 
    '''

    _DOC_ATTR = {
    'name': 'The text string that names the entity in the query and in the results.',             
    }
    
    def __init__(self, query):
        self.name = None
        self.query = query
    
    def __getattr__(self, name):
        return Property(self.query, self, name)
    
    def __eq__(self, other):
        return (isinstance(other, self.__class__)
            and self.__dict__ == other.__dict__)

    def __ne__(self, other):
        return not self.__eq__(other)
    
    def __key(self):
        return tuple(self.__dict__.values())
    
    def __hash__(self):
        return hash(self.__key())

    def _addName(self, name):
        while not name:
            entityType = self._type
            if entityType == '*':
                entityType = 'Generic'
            testName = '%s%04d' % (entityType, random.randint(0, 9999))
            if testName not in self.query._usedNames:
                name = testName
        if name in self.query._usedNames:
            raise ValueError('The name "%s" is already being used.' % name)
        self.name = name
        self.query._usedNames.append(name)

class Node(Entity):
    '''An object that represents a database node in a query.
    
    If the Node is created without a nodeType, the node will match any type.
    The list of available node types is available
    from the :meth:`Database.printStructure` method.
    
    A name attribute will be randomly generated unless a `name` argument
    is given. There is no particular reason to name objects unless we want to control 
    the query text exactly.

    Each node in a Neo4j database has a numeric ID, and passing that ID to the `nodeId` argument
    will save it in the Node's `id` attribute.
    If a Node with an ID number is passed to the :meth:`Query.setStartNode` method,
    that ID will be used to target the search. Otherwise the `id` attribute is ignored.
    
    Any properties of a node type can be accessed as an attribute of the Node. Accessing a
    Node's attribute will return a :class:`Property` object for that node property, which can be
    used with the :meth:`Query.addComparisonFilter` and :meth:`Query.addReturns` methods.
    '''
    
    _DOC_ATTR = {
    'nodeType': 'The type of database node this object represents.',
    'id': 'The numeric ID of this node in the database (default=None).'
    }

    def __init__(self, query, nodeType=None, name=None, nodeId=None):
        Entity.__init__(self, query)
        if not nodeType:
            nodeType = '*'
        self._type = self.nodeType = nodeType
        self.id = nodeId
        self._addName(name)

    def __repr__(self):
        return self.name    
        
class Relationship(Entity):
    '''An object that represents a database relationship in a query.
    
    If the Relationship is created without a relationType, the relationship will match any type.
    The list of available relationship types is available
    from the :meth:`Database.printStructure` method.
    
    The `start` and `end` arguments take :class:`Node` objects, and allow the relationship 
    to be connected to other nodes in the query. If either is omitted, a new node object
    is created implicitly to fill it.

    A name attribute will be randomly generated unless a `name` argument
    is given. There is no particular reason to name objects unless we want to control 
    the query text exactly.

    Any properties of a relationship type can be accessed as an attribute of the
    Relationship. Accessing a Relationship's attribute will return a
    :class:`Property` object for that relationship property, which can be used
    with the :meth:`Query.addComparisonFilter` and :meth:`Query.addReturns`
    methods.
    
    Setting the `optional` argument to True will cause the query pattern to match even
    if this relationship doesn't exist in a particular instance.
    '''

    _DOC_ATTR = {
    'relationType': 'The type of database relationship this object represents.',
    'start': 'The start :class:`Node` of this relationship.',
    'end': 'The end :class:`Node` of this relationship',
    'optional': "Whether the query pattern will match even if this relationship doesn't exist in a particular instance"
    }

    def __init__(self, query, relationType=None, start=None, end=None, name=None, optional=False):
        Entity.__init__(self, query)
        if not relationType:
            relationType = '*'
        self._type = self.relationType = relationType
        self.start = start or Node(query)
        self.end = end or Node(query)
        self.optional = ''
        if optional:
            self.optional = '?'
        self._addName(name)

    def __repr__(self):
        return '(%s)-[%s%s:%s]->(%s)' % (self.start, self.name, self.optional, self.relationType, self.end)

class Property(Entity):
    '''An object representing a property of a node or relationship in the database.
    
    Accessing an attribute of a :class:`Node` or :class:`Relationship` will return a
    :class:`Property` object, which can be used
    with the :meth:`Query.addComparisonFilter` and :meth:`Query.addReturns`
    methods.
    '''
    
    def __init__(self, query, parent, name):
        Entity.__init__(self, query)
        self.parent = parent
        self.name = name
        
    def __repr__(self):
        return '%s.%s' % (self.parent.name, self.name)
    
    def __getattr__(self):
        raise AttributeError
        
class Filter(Entity):
    '''An object representing a filter phrase in a query, which is a condition 
    that must be true for the query to match.
    
    The `pre` and `post` arguments are the values that are being tested, and should each be either:
    
    * an attribute of a :class:`Node` or :class:`Relationship` (which will return a
      :class:`Property` object), or
    * some text or a number.
     
    The list of available node and relationship properties can be obtained 
    from the :meth:`Database.printStructure` method.
    
    The `operator` argument is a text string of the comparison we want to test
    between the two properties. The available comparison operators are
    `=, <>, <, >, <=,` and `>=`.
    '''
    
    def __init__(self, query, pre, operator, post):
        Entity.__init__(self, query)
        self.pre = pre
        self.operator = operator
        self.post = post
        
    def __repr__(self):
        operands = []
        for operand in (self.pre, self.post):
            if isinstance(operand, (str, bool)):
                text = '"%s"' % operand
                operands.append(text)
            else:
                operands.append(str(operand))
        return '%s %s %s' % (operands[0], self.operator, operands[1])
    
    def __getattr__(self):
        raise AttributeError

class Moment(base.Music21Object):
    '''This object is similar in purpose to a
    :class:`~music21.voiceLeading.VerticalSlice` in that it contains every
    :class:`~music21.note.Note` that occurs at a given offset in a
    :class:`~music21.stream.Score`. (Notes are stored as references
    to the original objects in :class:`~weakref.WeakSet` objects.) A Moment acts like a
    :class:`~music21.spanner.Spanner` placed at the end of a Score.
    
    A Moment can serve the same function as a VerticalSlice or a call 
    to :meth:`~music21.stream.Stream.getElementsByOffset` on a flattened Score,
    providing easy access to all the Notes active at a particular offset.
    It is also a place to store information about that moment in the score, such as
    chord quality or pitch-class set.
    
    When a Score with Moments is added to a :class:`Database` object, it will
    add vertical relationships between Notes (`NoteSimultaneousWithNote`). It
    will also add Moment nodes to the database, along with their corresponding
    Note relationships (`NoteStartsAtMoment` and `NoteSustainedAtMoment`).
    
    Typically Moments are added to a score via the :meth:`musicNet.addMomentsToScore` 
    function. 
    '''
    
    _DOC_ORDER = ['getComponents', 'addComponents']
    
    _DOC_ATTR = {
    'sameOffset': 'A :class:`weakref.WeakSet` referring to all the Notes starting at the Moment.',
    'simultaneous': 'A :class:`weakref.WeakSet` referring to any Notes that started before the Moment but hold over into it.'
    }
        
    def __init__(self, components=None, *arguments):
        base.Music21Object.__init__(self)
        self.sameOffset = weakref.WeakSet()
        self.simultaneous = weakref.WeakSet()
#        self.nodeRef = None
        if components:
            self.addComponents(components, *arguments)
        
    def getComponents(self):
        '''Returns the contents of the object as a :class:`weakref.WeakSet`. This is simply the
        union of two of the object's attributes: `sameOffset` and `simultaneous`.
        '''
        return self.sameOffset | self.simultaneous
    
    def addComponents(self, components, *arguments):
        '''Adds a :class:`~music21.note.Note` object (or a list of Notes) to the
        object. If a Note has the same offset as this object, a reference to it
        is added to `sameOffset`. Otherwise a reference is added to `simultaneous`.
        '''
        if not common.isListLike(components):
            components = [components]
        components += arguments
        for c in components:
            if not isinstance(c, note.Note):
                raise ValueError('cannot add a non-Note object to a Moment')
            offset = c.getContextByClass('Measure').offset + c.offset
            if offset == self.offset:
                self.sameOffset.add(c)
            else:
                self.simultaneous.add(c)

class Test(unittest.TestCase):

    def runTest(self):
        pass
    
class TestExternal(unittest.TestCase):
    def runTest(self):
        pass

_DOC_ORDER = [Query, Database, Entity, Node, Relationship, Property, Filter, Moment]

if __name__ == "__main__":
    _prepDoctests()
    music21.mainTest(Test)

# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. 
# If a copy of the MPL was not distributed with this file, You can obtain one at 
# http://mozilla.org/MPL/2.0/.
