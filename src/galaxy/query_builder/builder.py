# Copyright (C) 2021 Humanitarian OpenStreetmap Team

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# Humanitarian OpenStreetmap Team
# 1100 13th Street NW Suite 800 Washington, D.C. 20005
# <info@hotosm.org>

from psycopg2 import sql
from json import dumps

HSTORE_COLUMN = "tags"


def create_hashtag_filter_query(project_ids, hashtags, cur, conn):
    '''returns hastag filter query '''

    merged_items = [*project_ids, *hashtags]

    filter_query = "({hstore_column} -> %s) ~~ %s"

    hashtag_filter_values = [
        *[f"%hotosm-project-{i};%" for i in project_ids],
        *[f"%{i};%" for i in hashtags],
    ]
    hashtag_tags_filters = [
        cur.mogrify(filter_query, ("hashtags", i)).decode()
        for i in hashtag_filter_values
    ]

    comment_filter_values = [
        *[f"%hotosm-project-{i} %" for i in project_ids],
        *[f"%{i} %" for i in hashtags],
    ]
    comment_tags_filters = [
        cur.mogrify(filter_query, ("comment", i)).decode()
        for i in comment_filter_values
    ]

    # Include cases for hasthags and comments found at the end of the string.
    no_char_filter_values = [
        *[f"%hotosm-project-{i}" for i in project_ids],
        *[f"%{i}" for i in hashtags],
    ]
    no_char_filter_values = [
        [cur.mogrify(filter_query, (k, i)).decode() for k in ("hashtags", "comment")]
        for i in no_char_filter_values
    ]

    no_char_filter_values = [item for sublist in no_char_filter_values for item in sublist]

    hashtag_filter = [*hashtag_tags_filters, *comment_tags_filters, *no_char_filter_values]

    hashtag_filter = [
        sql.SQL(f).format(hstore_column=sql.Identifier(HSTORE_COLUMN))
        for f in hashtag_filter
    ]

    hashtag_filter = sql.SQL(" OR ").join(hashtag_filter).as_string(conn)

    return hashtag_filter


def create_timestamp_filter_query(column_name,from_timestamp, to_timestamp, cur):
    '''returns timestamp filter query '''

    timestamp_column = column_name
    # Subquery to filter changesets matching hashtag and dates.
    timestamp_filter = sql.SQL("{timestamp_column} between %s AND %s").format(
        timestamp_column=sql.Identifier(timestamp_column))
    timestamp_filter = cur.mogrify(timestamp_filter,
                                   (from_timestamp, to_timestamp)).decode()

    return timestamp_filter


def create_changeset_query(params, conn, cur):
    '''returns the changeset query'''

    hashtag_filter = create_hashtag_filter_query(params.project_ids,
                                                 params.hashtags, cur, conn)
    timestamp_filter = create_timestamp_filter_query("created_at",params.from_timestamp,
                                                     params.to_timestamp, cur)

    changeset_query = f"""
    SELECT user_id, id as changeset_id, user_name as username
    FROM osm_changeset
    WHERE {timestamp_filter} AND ({hashtag_filter})
    """

    return changeset_query, hashtag_filter, timestamp_filter


def create_osm_history_query(changeset_query, with_username):
    '''returns osm history query'''

    column_names = [
        f"(each({HSTORE_COLUMN})).key AS feature",
        "action",
        "count(distinct id) AS count",
    ]
    group_by_names = ["feature", "action"]

    if with_username is True:
        column_names.append("username")
        group_by_names.extend(["user_id", "username"])

    order_by = (["count DESC"]
                if with_username is False else ["user_id", "action", "count"])
    order_by = ", ".join(order_by)

    columns = ", ".join(column_names)
    group_by_columns = ", ".join(group_by_names)

    query = f"""
    WITH T1 AS({changeset_query})
    SELECT {columns} FROM osm_element_history AS t2, t1
    WHERE t1.changeset_id = t2.changeset
    GROUP BY {group_by_columns} ORDER BY {order_by}
    """

    return query


def create_userstats_get_statistics_with_hashtags_query(params,con,cur):
        changeset_query, _, _ = create_changeset_query(params, con, cur)

        # Include user_id filter.
        changeset_query = f"{changeset_query} AND user_id = {params.user_id}"

        base_query = """
            SELECT (each(osh.tags)).key as feature, osh.action, count(distinct osh.id)
            FROM osm_element_history AS osh, T1
            WHERE osh.timestamp BETWEEN %s AND %s
            AND osh.uid = %s
            AND osh.type in ('way','relation')
            AND T1.changeset_id = osh.changeset
            GROUP BY feature, action
        """
        items = (params.from_timestamp, params.to_timestamp, params.user_id)
        base_query = cur.mogrify(base_query, items).decode()

        query = f"""
            WITH T1 AS (
                {changeset_query}
            )
            {base_query}
        """
        return query

def create_UserStats_get_statistics_query(params,con,cur):
        query = """
            SELECT (each(tags)).key as feature, action, count(distinct id)
            FROM osm_element_history
            WHERE timestamp BETWEEN %s AND %s
            AND uid = %s
            AND type in ('way','relation')
            GROUP BY feature, action
        """

        items = (params.from_timestamp, params.to_timestamp, params.user_id)
        query = cur.mogrify(query, items)
        return query


def create_users_contributions_query(params, changeset_query):
    '''returns user contribution query'''

    project_ids = ",".join([str(p) for p in params.project_ids])
    from_timestamp = params.from_timestamp.isoformat()
    to_timestamp = params.to_timestamp.isoformat()

    query = f"""
    WITH T1 AS({changeset_query}),
    T2 AS (
        SELECT (each(tags)).key AS feature,
            user_id,
            username,
            count(distinct id) AS count
        FROM osm_element_history AS t2, t1
        WHERE t1.changeset_id    = t2.changeset
        GROUP BY feature, user_id, username
    ),
    T3 AS (
        SELECT user_id,
            username,
            SUM(count) AS total_buildings
        FROM T2
        WHERE feature = 'building'
        GROUP BY user_id, username
    )
    SELECT user_id,
        username,
        total_buildings,
        public.tasks_per_user(user_id,
            '{project_ids}',
            '{from_timestamp}',
            '{to_timestamp}',
            'MAPPED') AS mapped_tasks,
        public.tasks_per_user(user_id,
            '{project_ids}',
            '{from_timestamp}',
            '{to_timestamp}',
            'VALIDATED') AS validated_tasks,
        public.editors_per_user(user_id,
            '{from_timestamp}',
            '{to_timestamp}') AS editors
    FROM T3;
    """
    return query


def generate_data_quality_hashtag_reports(cur, params):
    if params.hashtags is not None and len(params.hashtags) > 0:
        filter_hashtags = ", ".join(["%s"] * len(params.hashtags))
        filter_hashtags = cur.mogrify(sql.SQL(filter_hashtags), params.hashtags).decode()
        filter_hashtags = f"AND unnest_hashtags in ({filter_hashtags})"
    else:
        filter_hashtags = ""

    if params.geometry is not None:
        geometry_dump = dumps(dict(params.geometry))
        geom_filter = f"WHERE ST_CONTAINS(ST_GEOMFROMGEOJSON('{geometry_dump}'), location)"
    else:
        geom_filter = ""

    issue_types = ", ".join(["%s"] * len(params.issue_type))
    issue_types_str = [i for i in params.issue_type]
    issue_types = cur.mogrify(sql.SQL(issue_types), issue_types_str).decode()

    timestamp_filter = cur.mogrify(sql.SQL("created_at BETWEEN %s AND %s"), (params.from_timestamp, params.to_timestamp)).decode()

    query = f"""
        WITH t1 AS (SELECT osm_id, change_id, st_x(location) AS lat, st_y(location) AS lon, unnest(status) AS unnest_status from validation {geom_filter}),
        t2 AS (SELECT id, created_at, unnest(hashtags) AS unnest_hashtags from changesets WHERE {timestamp_filter})
        SELECT t1.osm_id,
            t1.change_id as changeset_id,
            t1.lat,
            t1.lon,
            t2.created_at,
            ARRAY_TO_STRING(ARRAY_AGG(t1.unnest_status), ',') AS issues
            FROM t1, t2 WHERE t1.change_id = t2.id
            {filter_hashtags}
            AND unnest_status in ({issue_types})
            GROUP BY t1.osm_id, t1.lat, t1.lon, t2.created_at, t1.change_id;
    """

    return query


def create_hashtagfilter_underpass(hashtags,columnname):
    """Generates hashtag filter query on the basis of list of hastags."""
    
    hashtag_filters = []
    for i in hashtags:
        if columnname =="username":
            hashtag_filters.append(f"""'{i}'={columnname}""")
        else:
            hashtag_filters.append(f"""'{i}'=ANY({columnname})""")
   
    join_query = " OR ".join(hashtag_filters)
    returnquery = f"""{join_query}"""
    
    return returnquery

def generate_data_quality_TM_query(params):
    '''returns data quality TM query with filters and parameteres provided'''
    print(params)
    hashtag_add_on="hotosm-project-"
    if "all" in params.issue_types:
        issue_types = ['badvalue','badgeom']
    else:
        issue_types=[]
        for p in params.issue_types:
            issue_types.append(str(p))
    
    change_ids=[]
    for p in params.project_ids:
        change_ids.append(hashtag_add_on+str(p)) 

    hashtagfilter=create_hashtagfilter_underpass(change_ids,"hashtags")
    status_filter=create_hashtagfilter_underpass(issue_types,"status")
    '''Geojson output query for pydantic model'''
    # query1 = """
    #     select '{ "type": "Feature","properties": {   "Osm_id": ' || osm_id ||',"Changeset_id":  ' || change_id ||',"Changeset_timestamp": "' || timestamp ||'","Issue_type": "' || cast(status as text) ||'"},"geometry": ' || ST_AsGeoJSON(location)||'}'
    #     FROM validation
    #     WHERE   status IN (%s) AND
    #             change_id IN (%s)
    # """ % (issue_types, change_ids)
    '''Normal Query to feed our OUTPUT Class '''
    query =f"""   with t1 as (
        select id
                From changesets 
                WHERE
                  {hashtagfilter}
            ),
        t2 AS (
             SELECT osm_id as Osm_id,
                change_id as Changeset_id,
                timestamp::text as Changeset_timestamp,
                status::text as Issue_type,
                ST_X(location::geometry) as lng,
                ST_Y(location::geometry) as lat

        FROM validation join t1 on change_id = t1.id
        WHERE
        {status_filter}
                )
        select *
        from t2
        """   
    return query


def generate_data_quality_username_query(params):
    
    '''returns data quality username query with filters and parameteres provided'''
    print(params)
    
    if "all" in params.issue_types:
        issue_types = ['badvalue','badgeom']
    else:
        issue_types=[]
        for p in params.issue_types:
            issue_types.append(str(p))
    
    osm_usernames=[]
    for p in params.osm_usernames:
        osm_usernames.append(p) 

    username_filter=create_hashtagfilter_underpass(osm_usernames,"username")
    status_filter=create_hashtagfilter_underpass(issue_types,"status")

    '''Normal Query to feed our OUTPUT Class '''
    query =f"""   with t1 as (
        select id,username as username
                From users 
                WHERE
                  {username_filter}
            ),
        t2 AS (
             SELECT osm_id as Osm_id,
                change_id as Changeset_id,
                timestamp::text as Changeset_timestamp,
                status::text as Issue_type,
                t1.username as username,
                ST_X(location::geometry) as lng,
                ST_Y(location::geometry) as lat
                
        FROM validation join t1 on user_id = t1.id  
        WHERE
        ({status_filter}) AND (timestamp between '{params.from_timestamp}' and  '{params.to_timestamp}')
                )
        select *
        from t2
        order by username
        """
    
    print(query)
    return query

def generate_mapathon_summary_underpass_query(params,cur):
    """Generates mapathon query from underpass"""
    projectid_hashtag_add_on="hotosm-project-"
    change_ids=[]
    for p in params.project_ids:
        change_ids.append(projectid_hashtag_add_on+str(p)) 

    projectidfilter=create_hashtagfilter_underpass(change_ids,"hashtags")
    hashtags=[]
    for p in params.hashtags:
        hashtags.append(str(p)) 
    hashtagfilter=create_hashtagfilter_underpass(hashtags,"hashtags")
    timestamp_filter=create_timestamp_filter_query("created_at",params.from_timestamp, params.to_timestamp,cur)

    base_where_query=f"""where  ({timestamp_filter}) """
    if hashtagfilter != '' and projectidfilter != '':
        base_where_query+= f"""AND ({hashtagfilter} OR {projectidfilter})"""
    elif hashtagfilter == '' and projectidfilter != '':
        base_where_query+= f"""AND ({projectidfilter})"""
    else:
        base_where_query+= f"""AND ({hashtagfilter})"""
    summary_query= f"""with t1 as (
        select  *
        from changesets
        {base_where_query})
        ,
        t2 as (
        select (each(added)).key as feature , (each(added)).value::Integer as count, 'create'::text as action
        from t1
        union all 
        select  (each(modified)).key as feature , (each(modified)).value::Integer as count, 'modify'::text as action
        from t1
        )
        select feature,action ,sum(count) as count
        from t2
        group by feature ,action 
        order by count desc """
    total_contributor_query= f"""select  COUNT(distinct user_id) as contributors_count
        from changesets
        {base_where_query}
        """
    # print(summary_query)
    # print("\n")

    # print(total_contributor_query)
    
    return summary_query,total_contributor_query

def generate_training_organisations_query():
    """Generates query for listing out all the organisations listed in training table from underpass
    """
    query= f"""select oid as id ,name
            from organizations
            order by oid """
    return query

def generate_filter_training_query(params):
    base_filter= []
    
    if params.oid:
        query = f"""(organization = {params.oid})"""
        base_filter.append(query)

    if params.topic_type:
        topic_type_filter = []
        for value in params.topic_type:
            query = f"""topictype = '{value}'"""
            topic_type_filter.append(query)
        join_query=" OR ".join(topic_type_filter)
        base_filter.append(f"""({join_query})""")

    if params.event_type:
        query = f"""(eventtype = '{params.event_type}')"""
        base_filter.append(query)

    if params.from_datestamp and params.to_datestamp:
        timestamp_query=f"""( date BETWEEN '{params.from_datestamp}'::date AND '{params.to_datestamp}'::date )"""
        base_filter.append(timestamp_query)

    if params.from_datestamp!= None and params.to_datestamp == None:
        timestamp_query=f"""( date >= '{params.from_datestamp}'::date )"""
        base_filter.append(timestamp_query)
    
    if params.to_datestamp!= None and params.from_datestamp == None:
        timestamp_query=f"""( date <= '{params.to_datestamp}'::date )"""
        base_filter.append(timestamp_query)

    filter_query=" AND ".join(base_filter)
    # print(filter_query)
    return filter_query

def generate_training_query(filter_query):
    base_query=f"""select * from training """
    if filter_query :
        base_query+=f"""WHERE {filter_query}"""
    return base_query
