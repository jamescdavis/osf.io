# -*- coding: utf-8 -*-
# Generated by Django 1.11.9 on 2018-02-22 20:39
from __future__ import unicode_literals

import time
import logging
import progressbar
from django.db import connection, migrations
from django.db.models import Q
from django.contrib.contenttypes.models import ContentType
from bulk_update.helper import bulk_update
from addons.wiki.models import WikiPage, NodeWikiPage, WikiVersion
from osf.models import Comment, Guid, AbstractNode

logger = logging.getLogger(__name__)

# Cache of WikiPage id => guid, of the form
# {
#     <id>: <guid_pk>
#
# }
WIKI_PAGE_GUIDS = {}

def reverse_func(state, schema):
    """
    Reverses NodeWikiPage migration. Repoints guids back to each NodeWikiPage,
    repoints comment_targets, comments_viewed_timestamps, and deletes all WikiVersions and WikiPages
    """
    nwp_content_type_id = ContentType.objects.get_for_model(NodeWikiPage).id

    nodes = AbstractNode.objects.exclude(wiki_pages_versions={})
    progress_bar = progressbar.ProgressBar(maxval=nodes.count()).start()
    for i, node in enumerate(nodes, 1):
        progress_bar.update(i)
        for wiki_key, version_list in node.wiki_pages_versions.iteritems():
            if version_list:
                for index, version in enumerate(version_list):
                    nwp = NodeWikiPage.objects.filter(former_guid=version).include(None)[0]
                    wp = WikiPage.load(version)
                    guid = migrate_guid_referent(Guid.load(version), nwp, nwp_content_type_id)
                    guid.save()
                    nwp = guid.referent
                move_comment_target(Guid.load(wp._id), nwp)
                update_comments_viewed_timestamp(node, wp, nwp)
    progress_bar.finish()
    WikiVersion.objects.all().delete()
    WikiPage.objects.all().delete()
    logger.info('NodeWikiPages restored and WikiVersions and WikiPages removed.')


def move_comment_target(current_guid, desired_target):
    """Move the comment's target from the current target to the desired target"""
    desired_target_guid_id = Guid.load(desired_target.former_guid).id
    if Comment.objects.filter(Q(root_target=current_guid) | Q(target=current_guid)).exists():
        Comment.objects.filter(root_target=current_guid).update(root_target_id=desired_target_guid_id)
        Comment.objects.filter(target=current_guid).update(target_id=desired_target_guid_id)
    return

def update_comments_viewed_timestamp(node, current_wiki_guid, desired_wiki_object):
    """Replace the current_wiki_object keys in the comments_viewed_timestamp dict with the desired wiki_object_id """
    users_pending_save = []
    # We iterate over .contributor_set instead of .contributors in order
    # to take advantage of .include('contributor__user')
    for contrib in node.contributor_set.all():
        user = contrib.user
        if user.comments_viewed_timestamp.get(current_wiki_guid, None):
            timestamp = user.comments_viewed_timestamp[current_wiki_guid]
            user.comments_viewed_timestamp[desired_wiki_object._id] = timestamp
            del user.comments_viewed_timestamp[current_wiki_guid]
            users_pending_save.append(user)
    if users_pending_save:
        bulk_update(users_pending_save, update_fields=['comments_viewed_timestamp'])
    return users_pending_save

def migrate_guid_referent(guid, desired_referent, content_type_id):
    """
    Point the guid towards the desired_referent.
    Pointing the NodeWikiPage guid towards the WikiPage will still allow links to work.
    """
    guid.content_type_id = content_type_id
    guid.object_id = desired_referent.id
    return guid

def migrate_node_wiki_pages(state, schema):
    create_wiki_pages_sql(state, schema)
    create_guids(state, schema)
    create_wiki_versions_and_repoint_comments_sql(state, schema)
    migrate_comments_viewed_timestamp_sql(state, schema)
    migrate_guid_referent_sql(state, schema)

def create_wiki_pages_sql(state, schema):
    then = time.time()
    logger.info('Starting migration of WikiPages [SQL]:')
    wikipage_content_type_id = ContentType.objects.get_for_model(WikiPage).id
    nodewikipage_content_type_id = ContentType.objects.get_for_model(NodeWikiPage).id
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TEMPORARY TABLE temp_wikipages
            (
              node_id INTEGER,
              user_id INTEGER,
              page_name_key TEXT,
              latest_page_name_guid TEXT,
              first_page_name_guid TEXT,
              page_name_display TEXT,
              created TIMESTAMP,
              modified TIMESTAMP
            )
            ON COMMIT DROP;

            -- Flatten out the wiki_page_versions json keys
            INSERT INTO temp_wikipages (node_id, page_name_key)
            SELECT
              oan.id AS node_id
              , jsonb_object_keys(oan.wiki_pages_versions) as page_name_key
            FROM osf_abstractnode AS oan;

            -- Retrieve the latest guid for the json key
            UPDATE temp_wikipages AS twp
            SET
              latest_page_name_guid = (
                  SELECT trim(v::text, '"')
                  FROM osf_abstractnode ioan
                    , jsonb_array_elements(oan.wiki_pages_versions->twp.page_name_key) WITH ORDINALITY v(v, rn)
                  WHERE ioan.id = oan.id
                  ORDER BY v.rn DESC
                  LIMIT 1
              )
            FROM osf_abstractnode AS oan
            WHERE oan.id = twp.node_id;

            -- Retrieve the first guid for the json key
            UPDATE temp_wikipages AS twp
            SET
              first_page_name_guid = (
                  SELECT trim(v::text, '"')
                  FROM osf_abstractnode ioan
                    , jsonb_array_elements(oan.wiki_pages_versions->twp.page_name_key) WITH ORDINALITY v(v, rn)
                  WHERE ioan.id = oan.id
                  ORDER BY v.rn ASC
                  LIMIT 1
              )
            FROM osf_abstractnode AS oan
            WHERE oan.id = twp.node_id;

            -- Remove any json keys that reference empty arrays (bad data? e.g. abstract_node id=232092)
            DELETE FROM temp_wikipages AS twp
            WHERE twp.latest_page_name_guid IS NULL;

            -- Retrieve page_name nodewikipage field for the latest wiki page guid
            UPDATE temp_wikipages AS twp
            SET
              page_name_display = anwp.page_name
            FROM osf_guid AS og INNER JOIN addons_wiki_nodewikipage AS anwp ON (og.object_id = anwp.id AND og.content_type_id = %s)
            WHERE og._id = twp.latest_page_name_guid;

            -- Retrieve user_id, created, and modified nodewikipage field for the first wiki page guid
            UPDATE temp_wikipages AS twp
            SET
              user_id = anwp.user_id
              , created = anwp.created
              , modified = anwp.modified
            FROM osf_guid AS og INNER JOIN addons_wiki_nodewikipage AS anwp ON (og.object_id = anwp.id AND og.content_type_id = %s)
            WHERE og._id = twp.first_page_name_guid;

            -- Populate the wikipage table
            INSERT INTO addons_wiki_wikipage (node_id, user_id, content_type_pk, page_name, created, modified)
            SELECT
              twp.node_id
              , twp.user_id
              , %s
              , twp.page_name_display
              , twp.created
              , twp.modified
            FROM temp_wikipages AS twp;
            """, [nodewikipage_content_type_id, nodewikipage_content_type_id, wikipage_content_type_id]
        )
    now = time.time()
    logger.info('Finished migration of WikiPages [SQL]: {:.5} seconds'.format(now - then))

def create_guids(state, schema):
    global WIKI_PAGE_GUIDS
    then = time.time()
    content_type = ContentType.objects.get_for_model(WikiPage)
    progress_bar = progressbar.ProgressBar(maxval=WikiPage.objects.count()).start()
    logger.info('Creating new guids for all WikiPages:')
    for i, wiki_page_id in enumerate(WikiPage.objects.values_list('id', flat=True), 1):
        # looping instead of bulk_create, so _id's are not the same
        progress_bar.update(i)
        guid = Guid.objects.create(object_id=wiki_page_id, content_type_id=content_type.id)
        WIKI_PAGE_GUIDS[wiki_page_id] = guid.id
    progress_bar.finish()
    now = time.time()
    logger.info('WikiPage guids created: {:.5} seconds'.format(now - then))
    return

def create_wiki_versions_and_repoint_comments_sql(state, schema):
    then = time.time()
    logger.info('Starting migration of WikiVersions [SQL]:')
    nodewikipage_content_type_id = ContentType.objects.get_for_model(NodeWikiPage).id
    wikipage_content_type_id = ContentType.objects.get_for_model(WikiPage).id
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TEMPORARY TABLE temp_wikiversions
            (
              node_id INTEGER,
              user_id INTEGER,
              page_name_key TEXT,
              wiki_page_id INTEGER,
              content TEXT,
              identifier INTEGER,
              created TIMESTAMP,
              modified TIMESTAMP,
              nwp_guid TEXT,
              latest_page_name_guid TEXT,
              page_name_display TEXT
            )
            ON COMMIT DROP;

            CREATE INDEX ON temp_wikiversions (nwp_guid ASC);
            CREATE INDEX ON temp_wikiversions (wiki_page_id ASC);

            -- Flatten out the wiki_page_versions arrays for each key
            INSERT INTO temp_wikiversions (node_id, page_name_key, nwp_guid, content, user_id, modified, identifier, created)
            SELECT
              oan.id as node_id,
              wiki_pages_versions.key,
              trim(nwp_guid::text, '"') as node_wiki_page_guid,
              nwp.content,
              nwp.user_id,
              nwp.modified,
              nwp.version as identifier,
              nwp.date as created
            FROM osf_abstractnode as oan,
              jsonb_each(oan.wiki_pages_versions) as wiki_pages_versions,
              jsonb_array_elements(wiki_pages_versions->wiki_pages_versions.key) as nwp_guid
            INNER JOIN addons_wiki_nodewikipage as nwp ON nwp.former_guid = trim(nwp_guid::text, '"');

            -- Retrieve the latest guid for the json key
            UPDATE temp_wikiversions AS twp
            SET
              latest_page_name_guid = (
                  SELECT trim(v::text, '"')
                  FROM osf_abstractnode ioan
                    , jsonb_array_elements(oan.wiki_pages_versions->twp.page_name_key) WITH ORDINALITY v(v, rn)
                  WHERE ioan.id = oan.id
                  ORDER BY v.rn DESC
                  LIMIT 1
              )
            FROM osf_abstractnode AS oan
            WHERE oan.id = twp.node_id;


            -- Retrieve page_name nodewikipage field for the latest wiki page guid
            UPDATE temp_wikiversions AS twb
            SET
              page_name_display = anwp.page_name
            FROM osf_guid AS og INNER JOIN addons_wiki_nodewikipage AS anwp ON (og.object_id = anwp.id AND og.content_type_id = %s)
            WHERE og._id = twb.latest_page_name_guid;

            -- Retrieve wiki page id
            UPDATE temp_wikiversions AS twc
            SET
                wiki_page_id = (
                    SELECT awp.id
                    FROM addons_wiki_wikipage as awp
                    WHERE (awp.node_id = twc.node_id
                        AND awp.page_name = twc.page_name_display)
                    LIMIT 1
                );

            -- Borrowed from https://gist.github.com/jamarparris/6100413
            CREATE OR REPLACE FUNCTION generate_object_id() RETURNS varchar AS $$
            DECLARE
                time_component bigint;
                machine_id bigint := FLOOR(random() * 16777215);
                process_id bigint;
                seq_id bigint := FLOOR(random() * 16777215);
                result varchar:= '';
            BEGIN
                SELECT FLOOR(EXTRACT(EPOCH FROM clock_timestamp())) INTO time_component;
                SELECT pg_backend_pid() INTO process_id;

                result := result || lpad(to_hex(time_component), 8, '0');
                result := result || lpad(to_hex(machine_id), 6, '0');
                result := result || lpad(to_hex(process_id), 4, '0');
                result := result || lpad(to_hex(seq_id), 6, '0');
                RETURN result;
            END;
            $$ LANGUAGE PLPGSQL;

            -- Populate the wiki_version table
            INSERT INTO addons_wiki_wikiversion (user_id, wiki_page_id, content, identifier, created, modified, _id)
            SELECT
              twv.user_id
              , twv.wiki_page_id
              , twv.content
              , twv.identifier
              , twv.created
              , twv.modified
              , generate_object_id()
            FROM temp_wikiversions AS twv;

            -- Migrate Comments on NodeWikiPages to point to WikiPages

            -- Create temporary view to store mapping of NodeWikiPage's Guid.pk => WikiPage.id
            CREATE VIEW nwp_guids_to_wp_id AS (
                SELECT
                osf_guid.id as nwp_guid_id,
                twv.wiki_page_id
                FROM osf_guid
                INNER JOIN temp_wikiversions twv ON (osf_guid._id = twv.nwp_guid)
                WHERE osf_guid._id = twv.nwp_guid
            );

            -- Use above view to construct a mapping between NodeWikiPage GUID pk => WikiPage GUID pk
            CREATE VIEW nwp_guids_to_wiki_page_guids as (
                SELECT
                nwp_guids_to_wp_id.nwp_guid_id as nwp_guid_id,
                osf_guid.id as wiki_page_guid_id
                FROM osf_guid
                INNER JOIN nwp_guids_to_wp_id ON (osf_guid.object_id = nwp_guids_to_wp_id.wiki_page_id)
                WHERE osf_guid.object_id = nwp_guids_to_wp_id.wiki_page_id AND osf_guid.content_type_id = %s
            );

            -- Change Comment.root_target from NodeWikiPages to their corresponding WikiPage
            UPDATE osf_comment
            SET
            root_target_id = (
                SELECT nwp_guids_to_wiki_page_guids.wiki_page_guid_id
                FROM nwp_guids_to_wiki_page_guids
                WHERE osf_comment.root_target_id = nwp_guids_to_wiki_page_guids.nwp_guid_id
                LIMIT 1
            )
            WHERE root_target_id IN (SELECT nwp_guid_id FROM nwp_guids_to_wiki_page_guids);

            -- Change Comment.target from NodeWikiPages to their corresponding WikiPage
            UPDATE osf_comment
            SET
            target_id = (
                SELECT nwp_guids_to_wiki_page_guids.wiki_page_guid_id
                FROM nwp_guids_to_wiki_page_guids
                WHERE osf_comment.target_id = nwp_guids_to_wiki_page_guids.nwp_guid_id
                LIMIT 1
            )
            WHERE target_id IN (SELECT nwp_guid_id FROM nwp_guids_to_wiki_page_guids);
            """, [nodewikipage_content_type_id, wikipage_content_type_id]
        )
    now = time.time()
    logger.info('Finished migration of WikiVersions [SQL]: {:.5} seconds'.format(now - then))

def migrate_comments_viewed_timestamp_sql(state, schema):
    then = time.time()
    logger.info('Starting migration of user comments_viewed_timestamp [SQL]:')
    wikipage_content_type_id = ContentType.objects.get_for_model(WikiPage).id
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE FUNCTION key_exists(json_field json, dictionary_key text)
            RETURNS boolean AS $$
            BEGIN
                RETURN (json_field->dictionary_key) IS NOT NULL;
            END;
            $$ LANGUAGE plpgsql;

            -- Defining a temporary table that has every update that needs to happen to users.
            -- Obsolete NodeWikiPage guids in comments_viewed_timestamp need to be replaced with
            -- corresponding new WikiPage guid
            -- Table has node_id, user_id, nwp_guid (old NodeWikiPage guid) and wp_guid (WikiPage guid)
            CREATE OR REPLACE FUNCTION update_comments_viewed_timestamp_sql()
              RETURNS SETOF varchar AS
            $func$
            DECLARE
              rec record;
            BEGIN
              FOR rec IN
                SELECT
                  oan.id as node_id,
                  osf_contributor.user_id as user_id,
                  (SELECT U0._id
                   FROM osf_guid AS U0
                   WHERE U0.object_id=wp.id AND U0.content_type_id = %s) AS wp_guid,
                  nwp_guid
                FROM osf_abstractnode as oan
                -- Joins contributor to node on contributor.node_id
                JOIN osf_contributor ON (oan.id = osf_contributor.node_id)
                JOIN osf_osfuser ON (osf_osfuser.id = user_id)
                -- Joins each of the wiki page key/version list from wiki_pages_versions
                LEFT JOIN LATERAL jsonb_each(oan.wiki_pages_versions) AS wiki_pages_versions ON TRUE
                -- Adds the last NWP id
                LEFT JOIN LATERAL cast(
                 (
                     SELECT trim(v::text, '"')
                     FROM osf_abstractnode ioan, jsonb_array_elements(wiki_pages_versions->wiki_pages_versions.key) WITH ORDINALITY v(v, rn)
                     WHERE ioan.id = oan.id
                     ORDER BY v.rn DESC
                     LIMIT 1
                 ) AS text) AS nwp_guid ON TRUE
                -- Joins the wiki page object, by finding the wiki page object on the node that has a page name similar to the key stored on wiki-pages versions
                -- Should work most of the time, there is some bad data though
                JOIN addons_wiki_wikipage AS wp ON (wp.node_id = oan.id) AND UPPER(wp.page_name::text) LIKE UPPER(wiki_pages_versions.key::text)
                WHERE oan.wiki_pages_versions != '{}' AND osf_osfuser.comments_viewed_timestamp != '{}' AND key_exists(osf_osfuser.comments_viewed_timestamp::json, nwp_guid)

              LOOP
                -- Loops through every row in temporary table above, and deletes old nwp_guid key and replaces with wp_guid key.
                -- Looping instead of joining to osf_user table because temporary table above has multiple rows with the same user
                UPDATE osf_osfuser
                SET comments_viewed_timestamp = comments_viewed_timestamp - rec.nwp_guid || jsonb_build_object(rec.wp_guid, comments_viewed_timestamp->rec.nwp_guid)
                WHERE osf_osfuser.id = rec.user_id;
              END LOOP;
            END
            $func$ LANGUAGE plpgsql;

            SELECT update_comments_viewed_timestamp_sql();
            """, [wikipage_content_type_id]
        )
    now = time.time()
    logger.info('Finished migration of comments_viewed_timestamp [SQL]: {:.5} seconds'.format(now - then))

def migrate_guid_referent_sql(state, schema):
    then = time.time()
    logger.info('Starting migration of Node Wiki Page guids, repointing them to Wiki Page guids [SQL]:')
    wikipage_content_type_id = ContentType.objects.get_for_model(WikiPage).id
    nodewikipage_content_type_id = ContentType.objects.get_for_model(NodeWikiPage).id
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TEMPORARY TABLE repoint_guids
            (
              node_id INTEGER,
              page_name_key TEXT,
              nwp_guid TEXT,
              latest_page_name_guid TEXT,
              wiki_page_id INTEGER,
              page_name_display TEXT
            )
            ON COMMIT DROP;

            -- Flatten out the wiki_page_versions arrays for each key
            INSERT INTO repoint_guids (node_id, page_name_key, nwp_guid)
            SELECT
              oan.id as node_id,
              wiki_pages_versions.key,
              trim(nwp_guid::text, '"') as node_wiki_page_guid
            FROM osf_abstractnode as oan,
              jsonb_each(oan.wiki_pages_versions) as wiki_pages_versions,
              jsonb_array_elements(wiki_pages_versions->wiki_pages_versions.key) as nwp_guid
            INNER JOIN addons_wiki_nodewikipage as nwp ON nwp.former_guid = trim(nwp_guid::text, '"');

            -- Retrieve the latest guid for the json key
            -- For example, if you have {'home': ['abcde', '12345', 'zyxwv']}, I need to preserve 'zyxwv'
            UPDATE repoint_guids AS twp
            SET
              latest_page_name_guid = (
                  SELECT trim(v::text, '"')
                  FROM osf_abstractnode ioan
                    , jsonb_array_elements(oan.wiki_pages_versions->twp.page_name_key) WITH ORDINALITY v(v, rn)
                  WHERE ioan.id = oan.id
                  ORDER BY v.rn DESC
                  LIMIT 1
              )
            FROM osf_abstractnode AS oan
            WHERE oan.id = twp.node_id;

            -- Retrieve page_name nodewikipage field for the latest wiki page guid (The latest one is most current because wikis can be renamed)
            UPDATE repoint_guids AS twb
            SET
              page_name_display = anwp.page_name
            FROM osf_guid AS og INNER JOIN addons_wiki_nodewikipage AS anwp ON (og.object_id = anwp.id AND og.content_type_id = %s)
            WHERE og._id = twb.latest_page_name_guid;

            -- Retrieve wiki page id using the node id and page name
            UPDATE repoint_guids AS twc
            SET
                wiki_page_id = (
                    SELECT awp.id
                    FROM addons_wiki_wikipage as awp
                    WHERE (awp.node_id = twc.node_id
                        AND awp.page_name = twc.page_name_display)
                    LIMIT 1
                );

            -- Update osf_guid by joining with temporary table repoint_guids.
            UPDATE osf_guid
            SET content_type_id = %s, object_id = wiki_page_id
            FROM repoint_guids
            WHERE repoint_guids.nwp_guid = osf_guid._id;
            """, [nodewikipage_content_type_id, wikipage_content_type_id]
        )
    now = time.time()
    logger.info('Finished repointing Node Wiki Page guids to Wiki Pages [SQL]: {:.5} seconds'.format(now - then))


class Migration(migrations.Migration):

    dependencies = [
        ('addons_wiki', '0009_auto_20180302_1404'),
    ]

    operations = [
        migrations.RunPython(migrate_node_wiki_pages, reverse_func),
    ]
