#!/bin/bash

# Uncomment both to have proxy env vars for  SUDO_WWW and SUDO_CMF INSTALL.sh commands 

# Proxy IP address
#PROXYHOST=1.2.3.4

# Proxy Port number
#PROXYPORT=3128

# if defined both
if [[ "${PROXYHOST}" != "" && ${PROXYPORT} != ""  ]]
then
	# Generate "env var=values " needed on one line.
	#   this is used to infix between sudo and the commands  to be run
	# expand to known proxy vars 
	RESULT=`eval echo -n "{http{,s}_proxy,HTTP_PROXY}=http://${PROXYHOST}:${PROXYPORT}/ "`
	# return as one line, to be used by INSTALL.sh as prefix for $SUDO_xxx commands 
	# Construct "env VAR=VALUE ... " with extra space
	env echo "env ${RESULT} "
else
	# always give a space (seperator) as result
	echo -n " "
fi
