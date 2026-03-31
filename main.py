# Replit entry point for AP Social Studies Dashboard
# This file imports and runs the Flask app

from ap_socsci_dashboard import app

if __name__ == '__main__':
    # Replit uses port 5000 and needs host 0.0.0.0 to be accessible
    app.run(host='0.0.0.0', port=5000, debug=False)
