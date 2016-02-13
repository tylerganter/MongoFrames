from blinker import signal
from bson.objectid import ObjectId
from datetime import date, datetime, time, timezone


class Frame:
    """
    A 'pin-weight' class for working with MongoDB collections. Frames provide
    support for:

    - Simple schema declaration (deliberate) through `_fields` attribute.
    - Selection with dereferencing (using projections).
    - Support for insert, insert_many, update, update_many, delete, delete_many.
    - Signals for insert, update, delete.
    - Dot syntax access to fields.
    - Helpers for cascade, nullify and pull.

    Frames are designed for simplicity and performance, it was originally
    created as a replacement for MongoEngine at Getme. We found we frequently
    wrote code to circumvent the MongoEngine internals (accessing pymongo
    directly) to achieve acceptable performance.
    """

    #
    _client = None

    #
    _db = None

    # IMPORTANT: These class attributes must be set in inheriting classes
    _collection = None
    _fields = []

    # A list of fields that should be ignored by to_dict
    _private_fields = []

    # Cache for dot syntax path conversions to keys (don't modify)
    _paths = {}

    def __init__(self, document=None):
        if not document:
            document = {}
        self._document = document

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return self._id == other._id

    def __hash__(self):
        return int(str(self._id), 16)

    def __lt__(self, other):
        return self._id < other._id

    @property
    def _pymongo_document(self):
        # Return a version of the document suitable for use with pymongo.
        return self._pymongo_safe(self._document)

    def to_dict(self):
        """Return a dictionary for the frame and document"""
        document_dict = self._json_safe(self._document)
        self._remove_keys(document_dict, self._private_fields)
        return document_dict

    # Get/Set attribute methods are overwritten to support for setting values
    # against the `_document`. Attribute names are converted to camelcase.

    def __getattr__(self, name):
        if '_document' in self.__dict__ and name in self.get_fields():
            return self.__dict__['_document'].get(name, None)
        raise AttributeError(
            "'{0}' has no attribute '{1}'".format(self.__class__.__name__, name)
            )

    def __setattr__(self, name, value):
        if '_document' in self.__dict__ and name in self.get_fields():
            self.__dict__['_document'][name] = value
            return
        super(Frame, self).__setattr__(name, value)

    # Operations

    def insert(self):
        """Insert a document"""

        assert '_id' not in self._document, "Can't insert documents with `_id`"

        # Send insert signal
        signal('insert').send(self.__class__, documents=[self])

        # Prepare the documents to be inserted
        document = self._pymongo_document

        # Insert the document and update the Id
        self._id = self.get_collection().insert_one(document).inserted_id

        # Send inserted signal
        signal('inserted').send(self.__class__, documents=[self])

    def update(self):
        """Update a document"""

        assert '_id' in self._document, "Can't update documents without `_id`"

        # Send update signal
        signal('update').send(self.__class__, documents=[self])

        # Prepare the documents to be updated
        document = self._pymongo_document

        # Update the document
        self.get_collection().update_one({'_id': self._id}, {'$set': document})

        # Send updated signal
        signal('updated').send(self.__class__, documents=[self])

    def delete(self):
        """Delete this document"""

        assert '_id' in self._document, "Can't delete documents without `_id`"

        # Send delete signal
        signal('delete').send(self.__class__, documents=[self])

        # Prepare the documents to be delete
        document = self._pymongo_document

        # Delete the document
        self.get_collection().delete_one({'_id': self._id})

        # Send deleted signal
        signal('deleted').send(self.__class__, documents=[self])

    @classmethod
    def insert_many(cls, documents):
        """Insert a list of documents"""

        # Ensure all documents have been converted to frames
        frames = cls._ensure_frames(documents)

        assert len([f for f in frames if '_id' in f._document]) == 0, \
                "Can't insert documents with `_id`s"

        # Send insert signal
        signal('insert').send(cls, documents=frames)

        # Prepare the documents to be inserted
        documents = [f._pymongo_document for f in frames]

        # Bulk insert
        ids = cls.get_collection().insert_many(documents).inserted_ids

        # Apply the Ids to the frames
        for i, id in enumerate(ids):
            frames[i]._id = id

        # Send inserted signal
        signal('inserted').send(cls, documents=frames)

    @classmethod
    def update_many(cls, documents):
        """Update multiple documents"""

        # Ensure all documents have been converted to frames
        frames = cls._ensure_frames(documents)

        all_count = len(documents)
        assert len([f for f in frames if '_id' in f._document]) == all_count, \
                "Can't update documents without `_id`s"

        # Send update signal
        signal('update').send(cls, documents=frames)

        # Prepare the documents to be updated
        documents = [f._pymongo_document for f in frames]

        # Update the documents
        for document in documents:
            cls.get_collection().update(
                {'_id': document['_id']}, {'$set': document})

        # Send updated signal
        signal('updated').send(cls.__class__, documents=frames)

    @classmethod
    def delete_many(cls, documents):
        """Delete multiple documents"""

        # Ensure all documents have been converted to frames
        frames = cls._ensure_frames(documents)

        all_count = len(documents)
        assert len([f for f in frames if '_id' in f._document]) == all_count, \
                "Can't delete documents without `_id`s"

        # Send delete signal
        signal('delete').send(cls, documents=frames)

        # Prepare the documents to be deleted
        ids = [f._id for f in frames]

        # Delete the documents
        cls.get_collection().delete_many({'_id': {'$in': ids}})

        # Send deleted signal
        signal('deleted').send(cls.__class__, documents=frames)

    @classmethod
    def _ensure_frames(cls, documents):
        """
        Ensure all items in a list are frames by converting those that aren't.
        """
        frames = []
        for document in documents:
            if not isinstance(document, Frame):
                frames.append(cls(document))
            else:
                frames.append(document)
        return frames

    # Querying

    @classmethod
    def count(cls, filter=None, **kwargs):
        """Return a count of documents matching the filter"""

        # Flatten the projection
        kwargs['projection'], references = \
                cls._flatten_projection(kwargs.get('projection'))

        if hasattr(filter, 'to_pymongo'):
            filter = filter.to_pymongo()

        return cls.get_collection().count(filter, **kwargs)

    @classmethod
    def one(cls, filter, **kwargs):
        """Find the first document matching the filter"""

        # Flatten the projection
        kwargs['projection'], references = \
                cls._flatten_projection(kwargs.get('projection'))

        # Find the document
        if hasattr(filter, 'to_pymongo'):
            filter = filter.to_pymongo()

        document = cls.get_collection().find_one(filter, **kwargs)

        # Make sure we found a document
        if not document:
            return

        # Dereference the document (if required)
        if references:
            cls._dereference([document], references)

        return cls(document)

    @classmethod
    def many(cls, filter=None, **kwargs):
        """Return a list of documents matching the filter"""

        # Flatten the projection
        kwargs['projection'], references = \
                cls._flatten_projection(kwargs.get('projection'))

        # Find the documents
        if hasattr(filter, 'to_pymongo'):
            filter = filter.to_pymongo()

        documents = list(cls.get_collection().find(filter, **kwargs))

        # Dereference the documents (if required)
        if references:
            cls._dereference(documents, references)

        return [cls(d) for d in documents]

    @classmethod
    def _dereference(cls, documents, references):
        """Dereference one or more documents"""

        # Dereference each reference
        for path, projection in references.items():

            # Collect Ids of documents to dereference
            ids = []
            for document in documents:
                value = cls._path_to_value(path, document)
                if not value:
                    continue

                if isinstance(value, ObjectId):
                    ids.append(value)
                else:
                    ids.extend(value)

            # Find the referenced documents
            if '$ref' not in projection:
                continue
            ref = projection.pop('$ref')

            frames = ref.many(
                {'_id': {'$in': ids}},
                projection=projection
                )
            frames = {f._id: f for f in frames}

            # Add dereferenced frames to the document
            for document in documents:
                value = cls._path_to_value(path, document)
                if not value:
                    continue

                if isinstance(value, ObjectId):
                    value = frames.get(value, None)
                else:
                    value = [frames[id] for id in value if id in frames]

                child_document = document
                keys = cls._path_to_keys(path)
                for key in keys[:-1]:
                    child_document = child_document[key]
                child_document[keys[-1]] = value

    @classmethod
    def _flatten_projection(cls, projection):
        """
        Flatten a structured projection (structure projections support for
        projections of (to be) dereferenced fields.

        projection = {
            'name': True,
            'email': True,
            'company': {
                '$ref': Company,
                'name': True
            }
        }
        """

        # If `projection` is empty return a full projection based on `_fields`
        if not projection:
            return {fn: True for fn in cls.get_fields()}, {}

        # Flatten the projection
        flat_projection = {}
        references = {}
        inclusive = True
        for key, value in projection.items():
            if isinstance(value, dict):
                # Store a reference projection
                references[key] = value
                flat_projection[key] = True

            elif key == '$ref':
                # Strip the $ref key
                continue

            else:
                # Store the root projection value
                flat_projection[key] = value
                inclusive = False

        # If only references where specified in the projection then return a
        # full projection based on `_fields`.
        if inclusive:
            flat_projection = {fn: True for fn in cls.get_fields()}

        return flat_projection, references

    # Integrity helpers

    @staticmethod
    def timestamp_insert(sender, documents=[]):
        """
        Timestamp the created and modified fields for all documents. This method
        should be bound to a frame class like so:

        ```
        MyFrameClass.listen('insert', MyFrameClass.insertStamp)
        ```
        """
        for document in documents:
            timestamp = datetime.now(timezone.utc)
            document.created = timestamp
            document.modified = timestamp

    @staticmethod
    def timestamp_update(sender, documents=[]):
        """
        Timestamp the modified field for all documents. This method should be
        bound to a frame class like so:

        ```
        MyFrameClass.listen('update', MyFrameClass.updateStamp)
        ```
        """
        for document in documents:
            document.modified = datetime.now(timezone.utc)

    @classmethod
    def cascade(cls, field, documents):
        """Apply a cascading delete (does not emit signals)"""
        cls.get_collection().delete_many(
            {field: {'$in': [d._id for d in documents]}}
            )

    @classmethod
    def nullify(cls, field, documents):
        """Nullify a reference field (does not emit signals)"""
        cls.get_collection().update_many(
            {field: {'$in': [d._id for d in documents]}},
            {'$set': {field: None}}
            )

    @classmethod
    def pull(cls, field, documents):
        """Pull references from a list field (does not emit signals)"""
        cls.get_collection().update_many(
            {field: {'$in': [d._id for d in documents]}},
            {'$pull:': {field: {'$in': ids}}}
            )

    # Signals

    @classmethod
    def listen(cls, event, func):
        """Add a callback for a signal against the class"""
        signal(event).connect(func, sender=cls)

    @classmethod
    def stop_listening(cls, event, func):
        """Add a callback for a signal against the class"""
        signal(event).disconnect(func, sender=cls)

    # Serializing

    @classmethod
    def _json_safe(cls, value):
        """Return JSON safe value"""
        # Date
        if type(value) == date:
            return str(value)

        # Datetime
        elif type(value) == datetime:
            return value.strftime('%Y-%m-%d %H:%M:%S')

        # Object Id
        elif isinstance(value, ObjectId):
            return str(value)

        # Frame
        elif isinstance(value, Frame):
            return value.to_dict()

        # Lists
        elif isinstance(value, (list, tuple)):
            return [cls._json_safe(v) for v in value]

        # Dictionaries
        elif isinstance(value, dict):
            return {k:cls._json_safe(v) for k, v in value.items()}

        return value

    @classmethod
    def _path_to_keys(cls, path):
        """Return a list of keys for a given path"""

        # Paths are cached for performance
        keys = cls._paths.get(path)
        if keys is None:
            keys = cls._paths[path] = path.split('.')

        return keys

    @classmethod
    def _path_to_value(cls, path, parent_dict):
        """Return a value from a dictionary at the given path"""
        keys = cls._path_to_keys(path)

        # Traverse to the tip of the path
        child_dict = parent_dict
        for key in keys[:-1]:
            child_dict = child_dict.get(key)
            if child_dict is None:
                return

        return child_dict.get(keys[-1])

    @classmethod
    def _pymongo_safe(cls, value):
        """Return a PyMongo safe value"""

        # Frame
        if isinstance(value, Frame):
            return value._id

        # Lists
        elif isinstance(value, (list, tuple)):
            return [cls._pymongo_safe(v) for v in value]

        # Dictionaries
        elif isinstance(value, dict):
            return {k: cls._pymongo_safe(v) for k, v in value.items()}

        return value

    @classmethod
    def _remove_keys(cls, parent_dict, paths):
        """
        Remove a list of keys from a dictionary.

        Keys are specified as a series of `.` separated paths for keys in child
        dictionaries, e.g 'parent_key.child_key.grandchild_key'.
        """

        for path in paths:
            keys = cls._path_to_keys(path)

            # Traverse to the tip of the path
            child_dict = parent_dict
            for key in keys[:-1]:
                child_dict = child_dict.get(key)

                if child_dict is None:
                    break

            if child_dict is None:
                continue

            # Remove the key
            if keys[-1] in child_dict:
                child_dict.pop(keys[-1])

    # Misc.

    @classmethod
    def get_collection(cls):
        """Return a reference to the database collection for the class"""
        return getattr(self.get_db(), cls._collection)

    @classmethod
    def get_db(cls):
        """Return the database for the collection"""
        if cls._db:
            return getattr(self._client, cls._db)
        return self._client.get_default_database()

    @classmethod
    def get_fields(cls):
        """Return the field names that can be set for the class"""

        # We set/use a cache for the output of this method as it's called every
        # time a fields value is requested using (.) dot notation and therefore
        # is performance critical.

        # Use the cached if set
        if hasattr(cls, '_get_fields_cache'):
            return cls._get_fields_cache

        # Set the cache if not
        cls._get_fields_cache = set(cls._fields + ['_id'])

        return cls._get_fields_cache