from __future__ import unicode_literals
import errno
import os
import re
import sys
import transaction
import uuid
from datetime import datetime, timedelta
from hashlib import sha1
from pyramid_addons.helpers import UTC
from sqla_mixins import BasicBase, UserMixin
from sqlalchemy import (Binary, Boolean, Column, DateTime, Enum, ForeignKey,
                        Integer, PickleType, String, Table, Unicode,
                        UnicodeText, func)
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, scoped_session, sessionmaker
from sqlalchemy.schema import UniqueConstraint
from zope.sqlalchemy import ZopeTransactionExtension

if sys.version_info < (3, 0):
    builtins = __import__('__builtin__')
else:
    import builtins

Base = declarative_base()
Session = scoped_session(sessionmaker(extension=ZopeTransactionExtension()))
# Make Session available to sqla_mixins
builtins._sqla_mixins_session = Session


testable_to_build_file = Table(
    'testable_to_build_file', Base.metadata,
    Column('testable_id', Integer, ForeignKey('testable.id'), nullable=False),
    Column('build_file_id', Integer, ForeignKey('buildfile.id'),
           nullable=False))

testable_to_execution_file = Table(
    'testable_to_execution_file', Base.metadata,
    Column('testable_id', Integer, ForeignKey('testable.id'), nullable=False),
    Column('execution_file_id', Integer, ForeignKey('executionfile.id'),
           nullable=False))

testable_to_file_verifier = Table(
    'testable_to_file_verifier', Base.metadata,
    Column('testable_id', Integer, ForeignKey('testable.id'), nullable=False),
    Column('file_verifier_id', Integer, ForeignKey('fileverifier.id'),
           nullable=False))

user_to_class = Table(
    'user_to_class', Base.metadata,
    Column('user_id', Integer, ForeignKey('user.id'), nullable=False),
    Column('class_id', Integer, ForeignKey('class.id'), nullable=False))

user_to_file = Table(
    'user_to_file', Base.metadata,
    Column('user_id', Integer, ForeignKey('user.id'), nullable=False),
    Column('file_id', Integer, ForeignKey('file.id'), nullable=False))

# which classes a user is an admin for
user_to_class_admin = Table('user_to_class_admin', Base.metadata,
                            Column('user_id', Integer, ForeignKey('user.id'),
                                   nullable=False),
                            Column('class_id', Integer, ForeignKey('class.id'),
                                   nullable=False))


class BuildFile(BasicBase, Base):
    __table_args__ = (UniqueConstraint('filename', 'project_id'),)
    file = relationship('File', backref='build_files')
    file_id = Column(Integer, ForeignKey('file.id'), nullable=False)
    filename = Column(Unicode, nullable=False)
    project_id = Column(Integer, ForeignKey('project.id'), nullable=False)

    def can_edit(self, user):
        """Return whether or not the user can edit the build file."""
        return self.project.can_edit(user)


class Class(BasicBase, Base):
    name = Column(Unicode, nullable=False, unique=True)
    projects = relationship('Project', backref='class_',
                            cascade='all, delete-orphan')

    def __repr__(self):
        return 'Class(name={0})'.format(self.name)

    def __str__(self):
        return 'Class Name: {0}'.format(self.name)

    def __cmp__(self, other):
        return cmp(self.name, other.name)

    def can_edit(self, user):
        """Return whether or not `user` can make changes to the class."""
        return user.is_admin or self in user.admin_for

    def can_view(self, user):
        """Return whether or not `user` can view the class."""
        return self.can_edit(user) or self in user.classes


class ExecutionFile(BasicBase, Base):
    __table_args__ = (UniqueConstraint('filename', 'project_id'),)
    file = relationship('File', backref='execution_files')
    file_id = Column(Integer, ForeignKey('file.id'), nullable=False)
    filename = Column(Unicode, nullable=False)
    project_id = Column(Integer, ForeignKey('project.id'), nullable=False)

    def can_edit(self, user):
        """Return whether or not the user can edit the build file."""
        return self.project.can_edit(user)


class File(BasicBase, Base):
    lines = Column(Integer, nullable=False)
    sha1 = Column(String, nullable=False, unique=True)
    size = Column(Integer, nullable=False)

    @staticmethod
    def fetch_or_create(data, base_path, sha1sum=None):
        if not sha1sum:
            sha1sum = sha1(data).hexdigest()
        file_ = File.fetch_by(sha1=sha1sum)
        if not file_:
            file_ = File(base_path=base_path, data=data, sha1=sha1sum)
            session = Session()
            session.add(file_)
            session.flush()  # Cannot commit the transaction here
        return file_

    @staticmethod
    def file_path(base_path, sha1sum):
        first = sha1sum[:2]
        second = sha1sum[2:4]
        return os.path.join(base_path, first, second, sha1sum[4:])

    def __init__(self, base_path, data, sha1):
        self.lines = 0
        for byte in data:
            if byte == '\n':
                self.lines += 1
        self.size = len(data)
        self.sha1 = sha1
        # save file
        path = File.file_path(base_path, sha1)
        try:
            os.makedirs(os.path.dirname(path))
        except OSError as error:
            if error.errno != errno.EEXIST:
                raise
        with open(path, 'wb') as fp:
            fp.write(data)

    def can_view(self, user):
        """Return true if the user can view the file."""
        # Perform simplest checks first
        if user.is_admin or self in user.files:
            return True
        elif user.admin_for:  # Begin more expensive comparisions
            # Single-indirect lookup
            classes = set(x.class_ for x in self.makefile_for_projects)
            if classes.intersection(user.admin_for):
                return True
            # Double indirect lookups
            classes = set(x.project.class_ for x in self.build_files)
            if classes.intersection(user.admin_for):
                return True
            classes = set(x.project.class_ for x in self.execution_files)
            if classes.intersection(user.admin_for):
                return True
            # Triple-indirect lookups
            classes = set(x.testable.project.class_ for x in self.expected_for)
            if classes.intersection(user.admin_for):
                return True
            classes = set(x.testable.project.class_ for x in self.stdin_for)
            if classes.intersection(user.admin_for):
                return True
            classes = set(x.submission.project.class_ for x in
                          self.submission_assocs)
            if classes.intersection(user.admin_for):
                return True
            # 4x-indirect lookups
            classes = set(x.test_case.testable.project.class_ for x
                          in self.test_case_result_for)
            if classes.intersection(user.admin_for):
                return True
        return False


class FileVerifier(BasicBase, Base):
    __table_args__ = (UniqueConstraint('filename', 'project_id'),)
    filename = Column(Unicode, nullable=False)
    min_size = Column(Integer, nullable=False)
    max_size = Column(Integer)
    min_lines = Column(Integer, nullable=False)
    max_lines = Column(Integer)
    optional = Column(Boolean, default=False, nullable=False)
    project_id = Column(Integer, ForeignKey('project.id'), nullable=False)
    warning_regex = Column(Unicode)

    def __cmp__(self, other):
        return cmp(self.filename, other.filename)

    def can_edit(self, user):
        return self.project.can_edit(user)

    def verify(self, base_path, file_):
        errors = []
        if file_.size < self.min_size:
            errors.append('must be >= {0} bytes'.format(self.min_size))
        elif self.max_size and file_.size > self.max_size:
            errors.append('must be <= {0} bytes'.format(self.max_size))
        if file_.lines < self.min_lines:
            errors.append('must have >= {0} lines'.format(self.min_lines))
        elif self.max_lines and file_.lines > self.max_lines:
            errors.append('must have <= {0} lines'.format(self.max_lines))

        if not self.warning_regex:
            return errors, None

        regex = re.compile(self.warning_regex)
        warnings = []
        for i, line in enumerate(open(File.file_path(base_path, file_.sha1))):
            for match in regex.findall(line):
                warnings.append({'lineno': i + 1, 'token': match})
        return errors, warnings


class VerificationResults(object):

    """Stores verification information about a single submission.

    WARNING: The attributes of this class cannot easily be changed as this
    class is pickled in the database.

    """

    def __init__(self):
        self._errors_by_filename = {}
        self._extra_filenames = None
        self._missing_to_testable_ids = {}
        self._warnings_by_filename = {}

    def __str__(self):
        import pprint
        return pprint.pformat(vars(self))

    def add_testable_id_for_missing_files(self, testable_id, missing_files):
        self._missing_to_testable_ids.setdefault(
            missing_files, set()).add(testable_id)

    def issues(self):
        """Return a mapping of filename to (warnings, errors) pairs"""
        errors = self._errors_by_filename
        warnings = self._warnings_by_filename
        retval = {}
        for filename in frozenset(errors.keys() + warnings.keys()):
            retval[filename] = (warnings.get(filename, []),
                                errors.get(filename, []))
        return retval

    def missing_testables(self):
        """Return a set of testables that have files missing."""
        ids = set()
        for id_set in self._missing_to_testable_ids.values():
            ids |= id_set
        return set(x for x in (Testable.fetch_by_id(y) for y in ids) if x)

    def set_errors_for_filename(self, errors, filename):
        self._errors_by_filename[filename] = errors

    def set_extra_filenames(self, filenames):
        self._extra_filenames = filenames

    def set_warnings_for_filename(self, warnings, filename):
        self._warnings_by_filename[filename] = warnings


class PasswordReset(Base):
    __tablename__ = 'passwordreset'
    created_at = Column(DateTime(timezone=True), default=func.now(),
                        nullable=False)
    reset_token = Column(Binary(length=16), primary_key=True)
    user = relationship('User', backref='password_reset')
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False,
                     unique=True)

    @classmethod
    def fetch_by(cls, **kwargs):
        session = Session()
        if 'reset_token' in kwargs:
            kwargs['reset_token'] = uuid.UUID(kwargs['reset_token']).bytes
        return session.query(cls).filter_by(**kwargs).first()

    @classmethod
    def generate(cls, user):
        pr = cls.fetch_by(user=user)
        if pr:
            retval = None
        else:
            retval = cls(reset_token=uuid.uuid4().bytes, user=user)
        return retval

    def get_token(self):
        return str(uuid.UUID(bytes=self.reset_token))


class Project(BasicBase, Base):
    __table_args__ = (UniqueConstraint('name', 'class_id'),)
    build_files = relationship(BuildFile, backref='project',
                               cascade='all, delete-orphan')
    class_id = Column(Integer, ForeignKey('class.id'), nullable=False)
    delay_minutes = Column(Integer, nullable=False, default=0)
    execution_files = relationship(ExecutionFile, backref='project',
                                   cascade='all, delete-orphan')
    file_verifiers = relationship('FileVerifier', backref='project',
                                  cascade='all, delete-orphan')
    makefile = relationship(File, backref='makefile_for_projects')
    makefile_id = Column(Integer, ForeignKey('file.id'), nullable=True)
    name = Column(Unicode, nullable=False)
    is_ready = Column(Boolean, default=False, nullable=False)
    submissions = relationship('Submission', backref='project',
                               cascade='all, delete-orphan')
    testables = relationship('Testable', backref='project',
                             cascade='all, delete-orphan')

    @property
    def delay(self):
        return timedelta(minutes=self.delay_minutes)

    def can_edit(self, user):
        """Return whether or not `user` can make changes to the project."""
        return self.class_.can_edit(user)

    def can_view(self, user):
        """Return whether or not `user` can view the project.

        The project's is_ready field must be set for a user to view.

        """
        return self.class_.can_edit(user) or \
            self.is_ready and self.class_ in user.classes

    def points_possible(self):
        """Return the total points possible for this project."""
        return sum([test_case.points
                    for testable in self.testables
                    for test_case in testable.test_cases])

    def verify_submission(self, base_path, submission):
        """Return list of testables that can be built."""
        results = VerificationResults()
        valid_files = set()
        file_mapping = dict([(x.filename, x) for x in submission.files])

        # Create a list of in-use file verifiers
        file_verifiers = set(fv for testable in self.testables
                             for fv in testable.file_verifiers)

        for fv in file_verifiers:
            if fv.filename in file_mapping:
                errors, warnings = fv.verify(base_path,
                                             file_mapping[fv.filename].file)
                if errors:
                    results.set_errors_for_filename(errors, fv.filename)
                else:
                    valid_files.add(fv.filename)
                if warnings:
                    results.set_warnings_for_filename(warnings, fv.filename)
                del file_mapping[fv.filename]
            elif not fv.optional:
                results.set_errors_for_filename(['file missing'], fv.filename)
        if file_mapping:
            results.set_extra_filenames(frozenset(file_mapping.keys()))

        # Determine valid testables
        retval = []
        for testable in self.testables:
            missing = frozenset(x.filename for x in testable.file_verifiers
                                if not x.optional) - valid_files
            if missing:
                results.add_testable_id_for_missing_files(testable.id, missing)
            elif testable.file_verifiers:
                retval.append(testable)

        # Reset existing attributes
        submission.test_case_results = []
        submission.testable_results = []
        # Set new information
        submission.verification_results = results
        submission.verified_at = func.now()
        return retval


class ProjectView(Base):
    __tablename__ = 'projectview'
    created_at = Column(DateTime(timezone=True), default=func.now(),
                        nullable=False)
    project_id = Column(Integer, ForeignKey('project.id'), primary_key=True,
                        nullable=False)
    project = relationship(Project)
    user_id = Column(Integer, ForeignKey('user.id'), primary_key=True,
                     nullable=False)
    user = relationship('User')

    @classmethod
    def fetch_by(cls, **kwargs):
        session = Session()
        return session.query(cls).filter_by(**kwargs).first()


class Submission(BasicBase, Base):
    files = relationship('SubmissionToFile', backref='submission')
    project_id = Column(Integer, ForeignKey('project.id'), nullable=False)
    test_case_results = relationship('TestCaseResult', backref='submission',
                                     cascade='all, delete-orphan')
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False)
    testable_results = relationship('TestableResult', backref='submission',
                                    cascade='all, delete-orphan')
    verification_results = Column(PickleType)
    verified_at = Column(DateTime(timezone=True), index=True)

    @property
    def extra_filenames(self):
        return self.verification_results._extra_filenames

    @staticmethod
    def earlier_submission_for_user(submission):
        """Return the submission immediately prior to the given submission."""
        return (Submission
                .query_by(project=submission.project, user=submission.user)
                .filter(Submission.created_at < submission.created_at)
                .order_by(Submission.created_at.desc()).first())

    @staticmethod
    def later_submission_for_user(submission):
        """Return the submission immediately prior to the given submission."""
        return (Submission
                .query_by(project=submission.project, user=submission.user)
                .filter(Submission.created_at > submission.created_at)
                .order_by(Submission.created_at).first())

    @staticmethod
    def merge_dict(d1, d2, on_collision):
        retval = {}
        for key in d1.keys():
            if key in d2:
                retval[key] = on_collision(d1[key], d2[key])
            else:
                retval[key] = d1[key]
        for key in d2.keys():
            if key not in retval:
                retval[key] = d2[key]
        return retval

    @staticmethod
    def most_recent_submission(project_id, user_id):
        """Return the most recent submission for the user and project id."""
        return (Submission.query_by(project_id=project_id, user_id=user_id)
                .order_by(Submission.created_at.desc()).first())

    def can_edit(self, user):
        """Return whether or not `user` can edit the submission."""
        return self.project.can_edit(user)

    def can_view(self, user):
        """Return whether or not `user` can view the submission."""
        return user == self.user or self.project.can_edit(user)

    def get_delay(self, update):
        """Return the minutes to delay the viewing of submission results.

        Only store information into the datebase when `update` is set.

        """
        now = datetime.now(UTC())
        zero = timedelta(0)
        delay = self.project.delay - (now - self.created_at)
        if delay <= zero:
            # Never delay longer than the project's delay time
            return None
        session = Session()
        pv = ProjectView.fetch_by(project=self.project, user=self.user)
        if not pv:  # Don't delay
            if update:
                pv = ProjectView(project=self.project, user=self.user)
                session.add(pv)
                session.flush()  # What if this fails?
            return None
        elif self.created_at <= pv.created_at:  # Always show older results
            return None
        pv_delay = self.project.delay - (now - pv.created_at)
        if pv_delay <= zero:
            if update:  # Update the counter
                pv.created_at = func.now()
                session.add(pv)
                session.flush()  # What if this fails?
            return None
        return min(delay, pv_delay).total_seconds() / 60

    def testable_statuses(self):
        """Return Status objects for non-pending Testables."""
        issues = self.verification_results.issues()
        with_build_errors = self.testables_with_build_errors()
        by_testable = {x.testable: x for x in self.testable_results}
        return [TestableStatus(testable, by_testable.get(testable),
                               issues, testable in with_build_errors)
                for testable in (set(self.project.testables)
                                 - self.testables_pending())]

    def testables_completed(self):
        """Return the set of testables that are done processing."""
        return set(x.testable for x in self.testable_results)

    def testables_pending(self):
        """Return the set of testables that _can_ execute and have yet to."""
        missing_testables = self.verification_results.missing_testables()
        return (set(self.project.testables) - missing_testables
                - self.testables_completed())

    def testables_succeeded(self):
        """Return the testables which have successfully executed."""
        return self.testables_completed() - self.testables_with_build_errors()

    def testables_with_build_errors(self):
        """Return the testables that had build errors.

        Build errors are indicated by testables which have TestableResult
        objects set (stores the Make output) and do not have TestCaseResults
        since these associations are updated at the same time.

        """
        return (self.testables_completed() -
                set(x.test_case.testable for x in self.test_case_results))

    def verify(self, base_path):
        """Verify the submission and return testables that can be executed."""
        return self.project.verify_submission(base_path, self)


class SubmissionToFile(Base):
    __tablename__ = 'submissiontofile'
    file = relationship(File, backref='submission_assocs')
    file_id = Column(Integer, ForeignKey('file.id'), nullable=False)
    filename = Column(Unicode, nullable=False, primary_key=True)
    submission_id = Column(Integer, ForeignKey('submission.id'),
                           primary_key=True, nullable=False)

    @staticmethod
    def fetch_file_mapping_for_submission(submission_id):
        '''Given a submission id, it returns a mapping of filenames
        to File objects'''
        results = Session().query(SubmissionToFile).filter_by(
            submission_id=submission_id)
        return dict([(stf.filename, stf.file) for stf in results])


class TestCase(BasicBase, Base):
    __table_args__ = (UniqueConstraint('name', 'testable_id'),)
    args = Column(Unicode, nullable=False)
    expected = relationship(File, primaryjoin='File.id==TestCase.expected_id',
                            backref='expected_for')
    expected_id = Column(Integer, ForeignKey('file.id'), nullable=True)
    name = Column(Unicode, nullable=False)
    output_filename = Column(Unicode, nullable=True)
    output_type = Column(Enum('diff', 'image', 'text', name='output_type'),
                         nullable=False)
    points = Column(Integer, nullable=False)
    source = Column(Enum('file', 'stderr', 'stdout', name='source'),
                    nullable=False)
    stdin = relationship(File, primaryjoin='File.id==TestCase.stdin_id',
                         backref='stdin_for')
    stdin_id = Column(Integer, ForeignKey('file.id'), nullable=True)
    testable_id = Column(Integer, ForeignKey('testable.id'), nullable=False)
    test_case_for = relationship('TestCaseResult', backref='test_case',
                                 cascade='all, delete-orphan')

    def __cmp__(self, other):
        return cmp(self.name, other.name)

    def can_edit(self, user):
        """Return whether or not `user` can make changes to the test_case."""
        return self.testable.project.can_edit(user)

    def serialize(self):
        data = dict([(x, getattr(self, x)) for x in ('args', 'id', 'source',
                                                     'output_filename')])
        if self.stdin:
            data['stdin'] = self.stdin.sha1
        else:
            data['stdin'] = None
        return data


class TestCaseResult(Base):
    """Stores information about a single run of a test case.

    The extra field stores the exit status when the status is `success`, and
    stores the signal number when the status is `signal`.

    When the TestCase output_type is not `diff` the diff file is actually
    the raw output file.

    """
    __tablename__ = 'testcaseresult'
    diff = relationship(File, backref='test_case_result_for')
    diff_id = Column(Integer, ForeignKey('file.id'), nullable=True)
    status = Column(Enum('nonexistent_executable', 'output_limit_exceeded',
                         'signal', 'success', 'timed_out',
                         name='status'), nullable=False)
    extra = Column(Integer)
    submission_id = Column(Integer, ForeignKey('submission.id'),
                           primary_key=True, nullable=False)
    test_case_id = Column(Integer, ForeignKey('testcase.id'),
                          primary_key=True, nullable=False)

    @classmethod
    def fetch_by_ids(cls, submission_id, test_case_id):
        session = Session()
        return session.query(cls).filter_by(
            submission_id=submission_id, test_case_id=test_case_id).first()

    def update(self, data):
        for attr, val in data.items():
            setattr(self, attr, val)
        self.created_at = func.now()


class TestableStatus(object):
    def __init__(self, testable, testable_results, verification_issues,
                 had_build_errors):
        self.testable = testable
        self.testable_results = testable_results
        self.issues = verification_issues
        self.had_build_errors = had_build_errors
        if had_build_errors:  # Add a build error message
            self.issues = {}
            for filename, (warnings, errors) in verification_issues.items():
                new = ['Build failed (see make output)'] + errors
                self.issues[filename] = (warnings, new)

    def has_make_output(self):
        return (self.testable_results and
                self.testable_results.make_results)

    def is_error(self):
        if self.had_build_errors:
            return True
        for _, errors in self.issues.values():
            if errors:
                return True
        return False


class Testable(BasicBase, Base):
    """Represents a set of properties for a single program to test."""
    __table_args__ = (UniqueConstraint('name', 'project_id'),)
    build_files = relationship(BuildFile, backref='testables',
                               secondary=testable_to_build_file)
    executable = Column(Unicode, nullable=False)
    execution_files = relationship(ExecutionFile, backref='testables',
                                   secondary=testable_to_execution_file)
    file_verifiers = relationship(FileVerifier, backref='testables',
                                  secondary=testable_to_file_verifier)
    make_target = Column(Unicode)  # When None, no make is required
    name = Column(Unicode, nullable=False)
    project_id = Column(Integer, ForeignKey('project.id'), nullable=False)
    test_cases = relationship('TestCase', backref='testable',
                              cascade='all, delete-orphan')
    testable_results = relationship('TestableResult', backref='testable',
                                    cascade='all, delete-orphan')

    def __cmp__(self, other):
        return cmp(self.name, other.name)

    def can_edit(self, user):
        """Return whether or not `user` can make changes to the testable."""
        return self.project.can_edit(user)

    def points(self):
        return sum([test_case.points for test_case in self.test_cases])


class TestableResult(BasicBase, Base):
    __table_args__ = (UniqueConstraint('submission_id', 'testable_id'),)
    make_results = Column(UnicodeText)
    submission_id = Column(Integer, ForeignKey('submission.id'),
                           nullable=False)
    testable_id = Column(Integer, ForeignKey('testable.id'), nullable=False)

    @staticmethod
    def fetch_or_create(make_results, **kwargs):
        tr = TestableResult.fetch_by(**kwargs)
        if tr:
            tr.created_at = func.now()
        else:
            tr = TestableResult(**kwargs)
        tr.make_results = make_results
        return tr


class User(UserMixin, BasicBase, Base):
    """The UserMixin provides the `username` and `password` attributes.
    `password` is a write-only attribute and can be verified using the
    `verify_password` function."""
    name = Column(Unicode, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    classes = relationship(Class, secondary=user_to_class, backref='users')
    files = relationship(File, secondary=user_to_file, backref='users')
    admin_for = relationship(Class, secondary=user_to_class_admin,
                             backref='admins')
    submissions = relationship('Submission', backref='user')

    @staticmethod
    def get_value(cls, value):
        '''Takes the class of the item that we want to
        query, along with a potential instance of that class.
        If the value is an instance of int or basestring, then
        we will treat it like an id for that instance.'''
        if isinstance(value, (basestring, int)):
            value = cls.fetch_by(id=value)
        return value if isinstance(value, cls) else None

    @staticmethod
    def login(username, password):
        """Return the user if successful, None otherwise"""
        retval = None
        try:
            user = User.fetch_by(username=username)
            if user and user.verify_password(password):
                retval = user
        except OperationalError:
            pass
        return retval

    def __cmp__(self, other):
        return cmp((self.name, self.username), (other.name, other.username))

    def __repr__(self):
        return 'User(username="{0}", name="{1}")'.format(self.username,
                                                         self.name)

    def __str__(self):
        admin_str = '(admin)' if self.is_admin else ''
        return 'Name: {0} Email: {1} {2}'.format(self.name, self.username,
                                                 admin_str)

    def can_view(self, user):
        """Return whether or not `user` can view information about the user."""
        return user.is_admin or self == user \
            or set(self.classes).intersection(user.admin_for)

    def classes_can_admin(self):
        """Return all the classes (sorted) that this user can admin."""
        if self.is_admin:
            return Session().query(Class).order_by(Class.name).all()
        else:
            return sorted(self.admin_for)

    def is_admin_for_any_class(self):
        return len(self.admin_for) > 0


def configure_sql(engine):
    """Configure session and metadata with the database engine."""
    Session.configure(bind=engine)
    Base.metadata.bind = engine


def create_schema(alembic_config_ini=None):
    """Create the database schema.

    :param alembic_config_ini: When provided, stamp with the current revision
    version.

    """
    Base.metadata.create_all()
    if alembic_config_ini:
        from alembic.config import Config
        from alembic import command
        alembic_cfg = Config(alembic_config_ini)
        command.stamp(alembic_cfg, 'head')


def populate_database():
    """Populate the database with some data useful for development."""
    if User.fetch_by(username='admin'):
        return

    # Admin user
    admin = User(name='Administrator', password='password',
                 username='admin', is_admin=True)
    # Class
    class_ = Class(name='CS32')
    Session.add(class_)
    Session.flush()

    # Project
    project = Project(name='Project 1', class_id=class_.id)
    Session.add(project)
    Session.flush()

    # File verification
    fv = FileVerifier(filename='test.c', min_size=3, min_lines=1,
                      project_id=project.id)

    Session.add_all([admin, fv])
    try:
        transaction.commit()
        print('Admin user created')
    except IntegrityError:
        transaction.abort()
