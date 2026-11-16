#!/bin/bash
# LDCT Simulation Monitor Script

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo "================================================"
echo "        LDCT Simulation Monitor"
echo "================================================"
echo ""

# Where the LDCT service is published on the host. Defaults to the port
# LDCT_Alsaihati/docker-compose.yml actually publishes (PORT_LDCT in the root
# .env, 8001) -- this used to point at 8000, the container-internal port, and
# so had never worked from the host.
LDCT_API="${LDCT_API:-http://localhost:${PORT_LDCT:-8001}}"

# Where the service writes its per-job logs on the host.
LDCT_LOG_DIR="${LDCT_LOG_DIR:-${API_STORAGE_DIR:-./API_storage}/ldct_alsaihati}"

# Function to check API status
check_api() {
    echo -e "${YELLOW}Checking API status...${NC}"
    curl -s "$LDCT_API/api/health" | python3 -m json.tool 2>/dev/null
    echo ""
}

# Function to list all jobs
list_jobs() {
    echo -e "${YELLOW}Active LDCT Jobs:${NC}"
    curl -s "$LDCT_API/api/ldct/jobs" | python3 -m json.tool
    echo ""
}

# Function to monitor specific job
monitor_job() {
    JOB_ID=$1
    echo -e "${YELLOW}Monitoring Job: $JOB_ID${NC}"
    echo ""

    while true; do
        RESPONSE=$(curl -s "$LDCT_API/api/ldct/status/$JOB_ID")

        STATUS=$(echo $RESPONSE | python3 -c "import sys, json; print(json.load(sys.stdin).get('status', 'unknown'))" 2>/dev/null)
        CURRENT_STEP=$(echo $RESPONSE | python3 -c "import sys, json; print(json.load(sys.stdin).get('current_step', 'N/A'))" 2>/dev/null)
        PROGRESS=$(echo $RESPONSE | python3 -c "import sys, json; print(json.load(sys.stdin).get('progress', 0))" 2>/dev/null)
        MESSAGE=$(echo $RESPONSE | python3 -c "import sys, json; print(json.load(sys.stdin).get('message', 'N/A'))" 2>/dev/null)

        clear
        echo "================================================"
        echo "        Job: $JOB_ID"
        echo "================================================"
        echo ""
        echo -e "Status:       ${GREEN}$STATUS${NC}"
        echo -e "Progress:     ${GREEN}${PROGRESS}%${NC}"
        echo -e "Current Step: ${YELLOW}$CURRENT_STEP${NC}"
        echo -e "Message:      $MESSAGE"
        echo ""
        echo "Press Ctrl+C to stop monitoring"

        if [ "$STATUS" == "completed" ]; then
            echo -e "${GREEN}✓ Simulation completed successfully!${NC}"
            break
        elif [ "$STATUS" == "failed" ]; then
            echo -e "${RED}✗ Simulation failed!${NC}"
            ERROR=$(echo $RESPONSE | python3 -c "import sys, json; print(json.load(sys.stdin).get('error', 'Unknown error'))" 2>/dev/null)
            echo -e "${RED}Error: $ERROR${NC}"
            break
        fi

        sleep 3
    done
}

# Function to tail log file
tail_log() {
    PATIENT_NAME=$1
    JOB_ID=$2
    LOG_FILE="${LDCT_LOG_DIR}/${PATIENT_NAME}/ldct_simulation_${JOB_ID}.log"

    if [ -f "$LOG_FILE" ]; then
        echo -e "${YELLOW}Tailing log file: $LOG_FILE${NC}"
        tail -f "$LOG_FILE"
    else
        echo -e "${RED}Log file not found: $LOG_FILE${NC}"
        echo "Make sure patient name and job ID are correct"
    fi
}

# Main menu
case "$1" in
    status)
        check_api
        ;;
    list)
        list_jobs
        ;;
    monitor)
        if [ -z "$2" ]; then
            echo -e "${RED}Error: Job ID required${NC}"
            echo "Usage: $0 monitor <JOB_ID>"
            exit 1
        fi
        monitor_job $2
        ;;
    log)
        if [ -z "$2" ] || [ -z "$3" ]; then
            echo -e "${RED}Error: Patient name and Job ID required${NC}"
            echo "Usage: $0 log <PATIENT_NAME> <JOB_ID>"
            exit 1
        fi
        tail_log $2 $3
        ;;
    *)
        echo "Usage: $0 {status|list|monitor|log}"
        echo ""
        echo "Commands:"
        echo "  status              - Check API health status"
        echo "  list                - List all LDCT jobs"
        echo "  monitor <JOB_ID>    - Monitor specific job in real-time"
        echo "  log <PATIENT> <ID>  - Tail log file for specific job"
        echo ""
        echo "Examples:"
        echo "  $0 status"
        echo "  $0 list"
        echo "  $0 monitor abc123-def456"
        echo "  $0 log DOE_JOHN abc123-def456"
        exit 1
        ;;
esac
