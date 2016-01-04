import argparse
import cjson
from collections import namedtuple, defaultdict
from cStringIO import StringIO
import errno
import glob
import gzip
import json
import logging
import os
from pyinstrument import Profiler
import sys


from edx.analytics.tasks.pathutil import PathSetTask
from edx.analytics.tasks.util import eventlog
from edx.analytics.tasks.util.deid_util import (
    backslash_encode_value,
    backslash_decode_value,
    needs_backslash_decoding,
    Deidentifier,
)


log = logging.getLogger(__name__)


ARTICLEREVISION_FIELDS = [
    'id',
    'revision_number',
    'user_message',
    'automatic_log',
    'ip_address',
    'user_id',
    'modified',
    'created',
    'previous_revision_id',
    'deleted',
    'locked',
    'article_id',
    'content',
    'title',
]

ArticleRevisionRecord = namedtuple('ArticleRevisionRecord', ARTICLEREVISION_FIELDS)  # pylint: disable=invalid-name


COURSEWARE_FIELDS = [
    'id',
    'module_type',
    'module_id',
    'student_id',
    'state',
    'grade',
    'created',
    'modified',
    'max_grade',
    'done',
    'course_id',
]

CoursewareRecord = namedtuple('CoursewareRecord', COURSEWARE_FIELDS)  # pylint: disable=invalid-name


# Fields in auth_userprofile per-course data export.
USERPROFILE_FIELDS = [
    'id',
    'user_id',
    'name',
    'language',
    'location',
    'meta',
    'courseware',
    'gender',
    'mailing_address',
    'year_of_birth',
    'level_of_education',
    'goals',
    'allow_certificate',
    'country',
    'city',
    'bio',
    'profile_image_uploaded_at',
]


UserProfileRecord = namedtuple('UserProfileRecord', USERPROFILE_FIELDS)  # pylint: disable=invalid-name

def load_user_profile(userprofile_path):
    result = {}
    with open(userprofile_path, 'r') as infile:
        for line in infile:
            fields = line.rstrip('\r\n').decode('utf8').split('\t')
            record = UserProfileRecord(*fields)
            result[record.user_id] = record
    return result


# Fields in custom "user-info" file (global, not per-course).
USERINFO_FIELDS = [
    'username',
    'email',
    'user_id',
    'name',
]


UserInfoRecord = namedtuple('UserInfoRecord', USERINFO_FIELDS)  # pylint: disable=invalid-name

def load_user_info(userinfo_path):
    """Reads a custom user-info file from the local fs that contains username, email, user-id, fullname."""
    result = {}
    with open(userinfo_path, 'r') as infile:
        next(infile, None)
        for line in infile:
            fields = line.rstrip('\r\n').decode('utf8').split('\t')
            # Once split, we can clean up the individual entries, and interpret the embedded newlines and tabs.
            # We'll also strip here, to remove the additional whitespace on usernames and fullnames.
            fields = [backslash_decode_value(field).strip() for field in fields]
            record = UserInfoRecord(*fields)
            # Store records twice, once with an int key, and once with a string key.
            # (They shouldn't collide.)
            result[int(record.user_id)] = record
            result[record.username] = record
    return result


def create_directory(output_dir):
    """Make sure a directory exists, creating parents as needed."""
    try:
        os.makedirs(output_dir)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            pass
        elif exc.errno != errno.EEXIST or os.path.isdir(output_dir):
            raise

# These event_type values are known to have the possibility that the
# user_id in context be different from the user_id in event payload.
# In these cases, the context user_id represents the user performing the
# action, while the user_id in the event payload represents the object.
EVENT_TYPES_WITH_DIFFERENT_USERIDS = [
    'edx.course.enrollment.activated',
    'edx.course.enrollment.deactivated',
    'edx.cohort.user_add_requested',
    'edx.cohort.user_removed',
    'edx.certificate.evidence_visited',
]

class BulkDeidentifier(object):

    parameters = {}

    user_profile = {}

    user_info = None

    deidentifier = None

    def __init__(self, **kwargs):
        log.info("Arguments = %s", kwargs)
        self.parameters = dict(kwargs)
        # This is global, so we can load it here.  (User-profile depends on the course.)
        if self.parameters['userinfo'] is not None:
            log.info("Loading user_info...")
            self.user_info = load_user_info(self.parameters['userinfo'])
            log.info("Loaded user_info...")

        # Just put all the parameters with true boolean values into the entity set.
        # It doesn't matter if there are extras.
        entity_list = [key for key, value in self.parameters.iteritems() if value == True]
        self.deidentifier = Deidentifier(log_context=self.parameters['log_context'], entities=set(entity_list))

    def deidentify_directory(self, input_dir, output_dir):
        if output_dir is not None:
            create_directory(output_dir)
        if self.parameters['wiki']:
            for filepath in glob.glob(os.path.join(input_dir, '*wiki_articlerevision-prod-analytics.sql')):
                self.deidentify_wiki_file(filepath, output_dir)
        if self.parameters['courseware']:
            for filepath in glob.glob(os.path.join(input_dir, '*courseware_studentmodule-prod-analytics.sql')):
                self.deidentify_courseware_file(filepath, output_dir)
        if self.parameters['forum']:
            for filepath in glob.glob(os.path.join(input_dir, '*.mongo')):
                self.deidentify_forum_file(filepath, output_dir)
        if self.parameters['event']:
            # This is generalized beyond localfs/glob.
            task = PathSetTask(src=[input_dir], include=['*-events-*.log.gz'])
            requirements = task.requires()
            for requirement in requirements:
                self.deidentify_event_file(requirement.output(), output_dir)

    def deidentify_event_file(self, input_target, output_dir):
        # Check for loading user_profile:
        user_profile = None
        self.missing_profile = defaultdict(int)

        input_filepath = input_target.path
        log.info(u"Deidentifying %s", input_filepath)
        with input_target.open('r') as infile:
            if input_filepath.endswith('.gz'):
                if input_filepath.startswith('s3'):
                    # We cannot read from S3 and use GZIP directly, so
                    # read into a buffer. (We assume the file is small
                    # enough to fit in memory.)
                    gzip_bytes = infile.read()
                    buffer = StringIO(gzip_bytes)
                    buffer.seek(0)
                    infile = gzip.GzipFile(fileobj=buffer)
                else:
                    infile = gzip.GzipFile(fileobj=infile)

            if output_dir is None:
                for line in infile:
                    self.deidentify_event_entry(line, user_profile)
            else:
                filename = os.path.basename(input_filepath)
                output_path = os.path.join(output_dir, filename)
                with open(output_path, 'w') as output_file:
                    with gzip.GzipFile(mode='wb', fileobj=output_file) as outfile:
                        for line in infile:
                            clean_line = self.deidentify_event_entry(line, user_profile)
                            outfile.write(clean_line)
                            outfile.write('\n')
        for key in sorted(self.missing_profile.iterkeys()):
            log.error(u"Missing profile entry for user_id '%s': %s", key, self.missing_profile[key])

    def get_userinfo_from_event(self, event, event_data):
        # Start simply, and just get obvious info.  See what it matches.
        # Need to check back on this, but we really only need to know
        # if this information is wrong.  What we want to come out
        # of this is a user_id and/or a username that can be used for
        # cleaning the rest of the event.

        # And actually, what we also need is the relevant fullname to use,
        # so we need to pick entries out of the user_info that match.
        # One or more?  No analysis was really made of alignment.
        # So we'll have to do it here...
        event_type = event.get('event_type')
        if isinstance(event_type, str):
            event_type = event_type.decode('utf8')
        debug_str = u" [event_type='{}']".format(event_type)

        username_entry = None
        username = eventlog.get_event_username(event)
        if username is not None:
            username = username.decode('utf8')
            if self.user_info is not None:
                username_entry = self.user_info.get(username)
                if username_entry is None:
                    log.error(u"username ('%s') is unknown to user_info %s", username, debug_str)

        # Get the user_id either as an int or None
        userid_entry = None
        user_id = event.get('context', {}).get('user_id')
        if user_id is not None and not isinstance(user_id, int):
            if len(user_id) == 0:
                user_id = None
            else:
                user_id = int(user_id)
        if user_id is not None:
            if self.user_info is not None:
                userid_entry = self.user_info.get(user_id)
                if userid_entry is None:
                    log.error(u"user_id ('%s') is unknown to user_info %s", user_id, debug_str)
                elif username_entry and userid_entry != username_entry:
                    log.error(u"user_id ('%s'='%s') does not match username ('%s'='%s') %s",
                        userid_entry.user_id, userid_entry.username, username_entry.username, username_entry.user_id, debug_str,
                    )

        event_userid_entry = None
        event_user_id = None
        if event_data and isinstance(event_data, dict):
            event_user_id = event_data.get('user_id')
            if event_user_id is not None and not isinstance(event_user_id, int):
                if len(event_user_id) == 0:
                    event_user_id = None
                else:
                    event_user_id = int(event_user_id)
            if event_user_id:
                if self.user_info is not None:
                    event_userid_entry = self.user_info.get(event_user_id)
                    if event_userid_entry is None:
                        log.error(u"Event_user_id ('%s') is unknown to user_info %s", event_user_id, debug_str)

                if user_id is None:
                    # This is way too common. In testing, every edx.course.enrollment.xxx had the user_id in the event but not
                    # in context.  Weird.
                    # log.warning(u"Found user_id ('%s') in event but nothing in context %s", event_user_id, debug_str)
                    pass
                elif event_userid_entry and userid_entry != event_userid_entry:
                    # This turns out to be somewhat expected for certain event types where one user is doing something on behalf
                    # of another user.  The actor is in context, and the object is in event payload.
                    if event_type not in EVENT_TYPES_WITH_DIFFERENT_USERIDS:
                        log.error(u"Context user_id ('%s'='%s') does not match event user_id ('%s'='%s') %s",
                                  userid_entry.user_id, userid_entry.username, event_userid_entry.username, event_userid_entry.user_id, debug_str,
                    )
                elif event_user_id != user_id:
                    log.error(u"Found user_id ('%s') in event that was different from context ('%s') %s", event_user_id, user_id, debug_str)

        # We choose the event user_id over the context, and fall back on the username.
        if event_userid_entry is not None:
            return event_userid_entry
        elif userid_entry is not None:
            return userid_entry
        else:
            return username_entry

    def deidentify_event_entry(self, line, user_profile):
        event = eventlog.parse_json_event(line)
        if event is None:
            # Unexpected here...
            log.error(u"Encountered event entry which failed to parse: %r", line)
            return line
        course_id = eventlog.get_course_id(event, from_url=True)
        if course_id is None:
            # Unexpected here...
            log.error(u"Encountered event entry with no course_id: %r", line)
            return line

        username = eventlog.get_event_username(event)
        event_source = event.get('event_source')
        event_type = event.get('event_type')

        # We cannot use this method as-is, since we need to know what was done to the event, so
        # that it can be transformed back to its original form once cleaned.
        # NOT event_data = eventlog.get_event_data(event)
        event_json_decoded = False
        event_data = event.get('event')

        if event_data is None:
            log.error(u"Encountered event entry with no 'event' payload: %r", line)
        if event_data == '':
            # Note that this happens with some browser events.  Instead of
            # failing to parse it as a JSON string, just leave as-is.
            pass
        elif isinstance(event_data, basestring):
            # Cjson produces str, while json produces unicode.  Hmm.
            if len(event_data) == 512 and 'POST' in event_data:
                # It's a truncated JSON string.  But we're going to throw it out anyway, so no worries.
                pass
            elif '{' not in event_data and '=' in event_data:
                # It's a key-value pair from a browser event.  Just process as-is, rather than parsing and reassembling.
                pass
            else:
                try:
                    event_data = eventlog.decode_json(event_data)
                    event_json_decoded = True
                except Exception:
                    log.error(u"Encountered event entry with unparseable 'event' payload: %r", line)

        # TODO: update the comment!  This is where we traverse the event in search of values that should be "cleansed".
        # Much along the model of what we already do for 'state' in CWSM.  Except that we need to be more
        # flexible in determining the level of backslash encoding -- decode and re-encode as many levels as needed
        # to get to strings that can be properly interpreted.
        event_user_info = self.get_userinfo_from_event(event, event_data)

        updated_event_data = self.deidentify_strings(event_data, u"event", username, event_user_info)
        if updated_event_data is not None:
            log.info(u"Deidentified %s event with event_type = '%s'", event_source, event_type)

            if event_json_decoded:
                # TODO: should really use cjson, if that were originally used for decoding the json.
                updated_event_data = json.dumps(updated_event_data)

            event['event'] = updated_event_data

        # TODO: should really use cjson, if that were originally used for decoding the json.
        return json.dumps(event)

    def deidentify_courseware_file(self, input_filepath, output_dir):
        # Check for loading user_profile:
        user_profile = None
        self.missing_profile = defaultdict(int)
        if self.parameters['fullname']:
            # convert input_filepath for courseware data to one that points to the corresponding userprofile file.
            userprofile_filepath = input_filepath.replace('courseware_studentmodule', 'auth_userprofile')
            log.info("Loading %s", userprofile_filepath)
            user_profile = load_user_profile(userprofile_filepath)

        if output_dir is None:
            log.info(u"Deidentifying %s", input_filepath)
            with open(input_filepath, 'r') as infile:
                for line in infile:
                    self.deidentify_courseware_entry(line, user_profile)
        else:
            filename = os.path.basename(input_filepath)
            output_path = os.path.join(output_dir, filename)
            log.info(u"Deidentifying %s to %s", input_filepath, output_path)

            with open(output_path, 'w') as outfile:
                with open(input_filepath, 'r') as infile:
                    for line in infile:
                        clean_line = self.deidentify_courseware_entry(line, user_profile)
                        outfile.write(clean_line)
                        outfile.write('\n')
        for key in sorted(self.missing_profile.iterkeys()):
            log.error(u"Missing profile entry for user_id '%s': %s", key, self.missing_profile[key])

    def deidentify_courseware_entry(self, line, user_profile):
        fields = line.rstrip('\r\n').decode('utf8').split('\t')
        record = CoursewareRecord(*fields)

        # Skip the header.
        if record.state == 'state':
            return line.rstrip('\r\n')

        profile_entry = None
        if user_profile is not None:
            user_id = record.student_id
            if user_id != 'NULL':
                profile_entry = user_profile.get(user_id)
                if profile_entry is None:
                    self.missing_profile[user_id] += 1

        # TODO: also read in auth_user, and store username for each user_id.
        pass

        # Courseware_studentmodule is not processed with the other SQL tables, so it
        # is not escaped in the same way.  In particular, we will not decode and encode it.
        state_str = record.state.replace('\\\\', '\\')
        try:
            state_dict = cjson.decode(state_str, all_unicode=True)
        except Exception as exc:
            log.exception(u"Unable to parse state as JSON for record %s: type = %s, state = %r", record.id, type(state_str), state_str)
            return line

        # Traverse the dictionary, looking for entries that need to be scrubbed.
        updated_state_dict = self.deidentify_strings(state_dict, u"state", None, profile_entry)
        if updated_state_dict is not None:
            # Can't reset values, so update original fields.
            updated_state = json.dumps(updated_state_dict).replace('\\', '\\\\')
            fields[4] = updated_state
            log.info(u"Deidentified state for user_id '%s' module_id '%s'", record.student_id, record.module_id)

        return u"\t".join(fields).encode('utf-8')

    def deidentify_wiki_file(self, input_filepath, output_dir):
        # Check for loading user_profile:
        user_profile = None
        if self.parameters['fullname']:
            # convert input_filepath for wiki data to one that points to the corresponding userprofile file.
            userprofile_filepath = input_filepath.replace('wiki_articlerevision', 'auth_userprofile')
            log.info("Loading %s", userprofile_filepath)
            user_profile = load_user_profile(userprofile_filepath)

        if output_dir is None:
            log.info(u"Deidentifying %s", input_filepath)
            with open(input_filepath, 'r') as infile:
                for line in infile:
                    self.deidentify_wiki_entry(line, user_profile)
        else:
            filename = os.path.basename(input_filepath)
            output_path = os.path.join(output_dir, filename)
            log.info(u"Deidentifying %s to %s", input_filepath, output_path)

            with open(output_path, 'w') as outfile:
                with open(input_filepath, 'r') as infile:
                    for line in infile:
                        clean_line = self.deidentify_wiki_entry(line, user_profile)
                        outfile.write(clean_line)
                        outfile.write('\n')

    def deidentify_wiki_entry(self, line, user_profile):
        fields = line.rstrip('\r\n').decode('utf8').split('\t')
        record = ArticleRevisionRecord(*fields)

        profile_entry = None
        if user_profile is not None:
            user_id = record.user_id
            if user_id != 'NULL':
                profile_entry = user_profile.get(user_id)
                if profile_entry is None:
                    log.error("Missing profile entry for user_id %s", user_id)

        if record.ip_address != 'NULL' and record.ip_address != 'ip_address':
            log.warning("Found non-NULL IP address")
        if record.automatic_log != '' and record.automatic_log != 'automatic_log':
            log.warning(u"Found non-zero-length automatic_log: %s", record.automatic_log)

        # Can't reset values, so update original fields.
        fields[12] = backslash_encode_value(self.deidentify_text(backslash_decode_value(record.content), None, profile_entry))
        fields[2] = backslash_encode_value(self.deidentify_text(backslash_decode_value(record.user_message), None, profile_entry))
        return u"\t".join(fields).encode('utf-8')

    def deidentify_forum_file(self, input_filepath, output_dir):
        # Check for loading user_profile:
        user_profile = None
        if self.parameters['fullname']:
            # convert input_filepath for forum data to one that points to the corresponding userprofile file.
            userprofile_filepath = input_filepath.replace('prod.mongo', 'auth_userprofile-prod-analytics.sql')
            log.info("Loading %s", userprofile_filepath)
            user_profile = load_user_profile(userprofile_filepath)

        if output_dir is None:
            log.info("Deidentifying %s", input_filepath)
            with open(input_filepath, 'r') as infile:
                for line in infile:
                    self.deidentify_forum_entry(line, user_profile)
        else:
            filename = os.path.basename(input_filepath)
            output_path = os.path.join(output_dir, filename)
            log.info("Deidentifying %s to %s", input_filepath, output_path)

            with open(output_path, 'w') as outfile:
                with open(input_filepath, 'r') as infile:
                    for line in infile:
                        clean_line = self.deidentify_forum_entry(line, user_profile)
                        outfile.write(clean_line)
                        outfile.write('\n')

    def deidentify_forum_entry(self, line, user_profile):
        # Round trip does not preserve content.  Original had no embedded spaces,
        # and entries were in alphabetic order.  This is addressed by modifying the
        # separators and setting sort_keys, but there are character encodings that
        # are also different, as to when \u notation is used for a character as
        # opposed to a utf8 encoding of the character.
        try:
            entry = cjson.decode(line, all_unicode=True)
        except ValueError as exc:
            log.error("Failed to parse json for line: %r", line)
            return ""

        # Clean the body of the forum post.
        body = entry['body']
        user_info = {'username': entry.get('author_username')}
        profile_entry = None
        if user_profile is not None:
            user_id = entry.get('author_id')
            profile_entry = user_profile.get(user_id)
            if profile_entry is None:
                log.error(u"Missing profile entry for user_id %s username %s", user_id, username)
            else:
                user_info['name'] = profile_entry.name
        clean_body = self.deidentify_text(body, username, profile_entry)
        entry['body'] = clean_body

        # Also clean the title, since it also contains username and fullname matches.
        title = entry['title']
        clean_title = self.deidentify_text(title, username, profile_entry)
        entry['title'] = clean_title

        return json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')

    def deidentify_text(self, text, username, user_profile_entry):
        # PLACEHOLDER SHIM
        user_info = {'username': username}
        if user_profile_entry:
            user_info['name'] = user_profile_entry.name
        return self.deidentifier.deidentify_text(text, user_info)

    def deidentify_strings(self, obj, label, username, user_profile_entry):
        # PLACEHOLDER SHIM
        user_info = {'username': username}
        if user_profile_entry:
            user_info['name'] = user_profile_entry.name
        return self.deidentifier.deidentify_structure(obj, label, user_info)


#####################

def main():
    """Command-line utility for using (and testing) s3 utility methods."""
    logging.basicConfig(level=logging.DEBUG)
    arg_parser = argparse.ArgumentParser(description='Perform deidentification of forum .mongo dump files.')

    arg_parser.add_argument(
        'input',
        help='Read mongo files from this location.',
    )
    arg_parser.add_argument(
        '-o', '--output',
        help='Write deidentified mongo files to this location in the local file system.',
        default=None
    )
    arg_parser.add_argument(
        '-u', '--userinfo',
        help='Read a custom user-info file from the local fs that contains username, email, user-id, fullname.',
        default=None
    )
    arg_parser.add_argument(
        '--log-context',
        help='characters on each side of match',
        type=int,
        default=50,
    )
    #####################
    # Flags to indicate what to deidentify.
    #####################
    arg_parser.add_argument(
        '--forum',
        help='Read in and deidentify forum posts.',
        action='store_true',
    )
    arg_parser.add_argument(
        '--wiki',
        help='Read in and deidentify wiki documents.',
        action='store_true',
    )
    arg_parser.add_argument(
        '--courseware',
        help='Read in and deidentify courseware_studentmodule records.',
        action='store_true',
    )
    arg_parser.add_argument(
        '--event',
        help='Read in and deidentify events.',
        action='store_true',
    )

    #####################
    # Various flags to indicate what to look for.
    #####################
    arg_parser.add_argument(
        '--phone',
        help='Extract phone numbers',
        action='store_true',
    )
    arg_parser.add_argument(
        '--possible-phone',
        help='Extract phone numbers',
        action='store_true',
    )
    arg_parser.add_argument(
        '--email',
        help='Extract email addresses',
        action='store_true',
    )
    arg_parser.add_argument(
        '--phone-context',
        help='Extract phone number context',
        action='store_true',
    )
    arg_parser.add_argument(
        '--email-context',
        help='Extract email address context',
        action='store_true',
    )
    arg_parser.add_argument(
        '--name-context',
        help='Extract name context',
        action='store_true',
    )    
    arg_parser.add_argument(
        '--facebook',
        help='Extract facebook urls',
        action='store_true',
    )    
    arg_parser.add_argument(
        '--username',
        help='Extract username',
        action='store_true',
    )    
    arg_parser.add_argument(
        '--fullname',
        help='Extract fullname.',
        action='store_true',
    )
    arg_parser.add_argument(
        '--skip-post',
        help='Skip performing filtering on event.POST entries.',
        action='store_true',
    )
    arg_parser.add_argument(
        '--pyinstrument',
        help='Profile the run and write the output to stderr',
        action='store_true'
    )
    args = arg_parser.parse_args()
    kwargs = vars(args)

    profiler = None
    if args.pyinstrument:
        profiler = Profiler() # or Profiler(use_signal=False), see below
        profiler.start()

    try:
        deid = BulkDeidentifier(**kwargs)
        deid.deidentify_directory(args.input, args.output)
    finally:
        if profiler:
            profiler.stop()
            print >>sys.stderr, profiler.output_text(unicode=True, color=True)


if __name__ == '__main__':
    main()
