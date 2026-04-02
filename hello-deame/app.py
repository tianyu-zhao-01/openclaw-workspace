from datetime import datetime

from flask import Flask, jsonify, render_template

app = Flask(__name__)


@app.route('/')
def hello_world():
    return render_template('index.html', title='Hello Deame')


@app.route('/api/time')
def api_time():
    now = datetime.now()
    return jsonify(
        {
            'iso': now.isoformat(timespec='seconds'),
            'display': now.strftime('%Y-%m-%d %H:%M:%S'),
        }
    )

if __name__ == '__main__':
    app.run(debug=True)