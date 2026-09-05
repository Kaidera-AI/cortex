-- Repair active documentation paths after the brand cutover.
-- Historical records keep their original path provenance.

BEGIN;

UPDATE agent_profiles
   SET profile_text = replace(
         regexp_replace(
           profile_text,
           E'3\\. keep the shipped vault summary layer current, especially:\\n(?:[[:space:]]*- `[^`]+`\\n){2}4\\. keep',
           E'3. keep the shipped vault summary layer current when product or architecture truth changes\n4. keep',
           'g'
         ),
         'Program/Kaidera AI/',
         'Program/'
       ),
       updated_at = NOW()
 WHERE project = 'kaidera-os'
   AND (
       profile_text LIKE '%Program/Kaidera AI/%'
       OR profile_text LIKE '%shipped vault summary layer current, especially:%'
   );

COMMIT;
