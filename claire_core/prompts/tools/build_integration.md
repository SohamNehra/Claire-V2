## build_integration -- write, check and install an integration of your own

Builds a connection to a service Claire has no integration for. Follow the
`building-integrations` skill: it has the steps, the manifest format, the kit,
and the rules. The actions:

- `new`: start one in this task's folder. Give <name>, <title>, <host> (the
  service's API host) and <description>.
- `edit`: copy an installed integration into this task's folder to change it.
- `check`: validate integration.json and run test.py in the sandbox, offline.
  Read every line it reports and fix the cause before checking again.
- `submit`: check again, then ask sir to approve installing it. Approved, it is
  installed and tested, and sir is asked for any credentials it declares.
- `status`: where installed integrations stand.

<build_integration>
<action>new</action>
<name>youtube</name>
<title>YouTube</title>
<host>www.googleapis.com</host>
<description>Search YouTube and read video and channel details.</description>
</build_integration>

<build_integration>
<action>check</action>
<name>youtube</name>
</build_integration>
