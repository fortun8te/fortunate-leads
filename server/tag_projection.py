"""Read-time fit labels from the existing verdict, without modifying model output.

The same relation backs display, filtering and counts. No backfill or model call is
needed when a verdict changes. Owner labels remain explicit owner facts.
"""
import owner


def parts():
    eligible = ("coalesce(v.tier,'unread')!='unread' AND v.content_fit IS NOT NULL "
                "AND coalesce(m.status,'') NOT IN ('client','no') "
                "AND NOT EXISTS (SELECT 1 FROM owner_context oc WHERE oc.person_id=v.person_id AND oc.relationships LIKE '%client%') "
                "AND NOT (coalesce(m.status,'')='' AND EXISTS "
                "(SELECT 1 FROM tags c WHERE c.person_id=v.person_id "
                "AND c.source='manual' AND lower(trim(c.tag))='client'))")
    fit = "CASE WHEN v.content_fit>=70 THEN 'Fit: strong' ELSE 'Fit: good' END"
    stored = ("SELECT t.person_id,t.tag,t.grp,t.source FROM tags t "
            "LEFT JOIN marks tm ON tm.person_id=t.person_id "
            "WHERE " + owner.visible_tag_sql() + " AND NOT "
            "(t.source='auto' AND t.tag IN ('Fit: strong','Fit: good')) "
            "AND (t.source!='auto' OR t.tag!='AI: Top fit' OR EXISTS "
            "(SELECT 1 FROM verdicts v LEFT JOIN marks m ON m.person_id=v.person_id "
            "WHERE v.person_id=t.person_id AND " + eligible + " AND v.role='buyer' AND v.content_fit>=75)) ")
    derived = ("SELECT v.person_id," + fit + " AS tag,'signal' AS grp,'auto' AS source "
            "FROM verdicts v LEFT JOIN marks m ON m.person_id=v.person_id "
            "WHERE " + eligible + " AND v.content_fit>=45 "
            "AND NOT EXISTS (SELECT 1 FROM tags own WHERE own.person_id=v.person_id "
            "AND own.tag=" + fit + " AND own.source!='auto')")

    human = " UNION ALL ".join(
        "SELECT oc.person_id,'" + label + "' AS tag,'relationship' AS grp,'manual' AS source FROM owner_context oc "
        "WHERE oc.relationships LIKE '%\"" + key + "\"%' AND NOT EXISTS (SELECT 1 FROM tags t "
        "WHERE t.person_id=oc.person_id AND t.tag='" + label + "' COLLATE NOCASE)"
        for key, label in owner.RELATIONSHIPS.items())
    return stored, derived, human


def relation():
    return " UNION ALL ".join(parts())
