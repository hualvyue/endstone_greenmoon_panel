import json
import os
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import jwt

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-change-in-production'

DATA_FILE = 'data.json'

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        'users': {},
        'posts': [],
        'next_post_id': 1
    }

def save_data(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def generate_token(username):
    payload = {
        'username': username,
        'exp': datetime.utcnow() + timedelta(days=1)
    }
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token:
            return jsonify({'msg': 'Missing token'}), 401
        try:
            if token.startswith('Bearer '):
                token = token[7:]
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            current_user = data['username']
        except jwt.ExpiredSignatureError:
            return jsonify({'msg': 'Token expired'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'msg': 'Invalid token'}), 401
        return f(current_user, *args, **kwargs)
    return decorated

@app.route('/register', methods=['POST'])
def register():
    req = request.get_json()
    username = req.get('username')
    password = req.get('password')
    email = req.get('email', '')

    if not username or not password:
        return jsonify({'msg': 'Username and password required'}), 400

    data = load_data()
    if username in data['users']:
        return jsonify({'msg': 'User already exists'}), 400

    hashed = generate_password_hash(password)
    data['users'][username] = {
        'password_hash': hashed,
        'email': email,
        'join_date': datetime.utcnow().isoformat()
    }
    save_data(data)
    return jsonify({'msg': 'User registered successfully'}), 201

@app.route('/login', methods=['POST'])
def login():
    req = request.get_json()
    username = req.get('username')
    password = req.get('password')

    data = load_data()
    user = data['users'].get(username)
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'msg': 'Invalid credentials'}), 401

    token = generate_token(username)
    return jsonify({'access_token': token}), 200

@app.route('/posts', methods=['POST'])
@token_required
def create_post(current_user):
    req = request.get_json()
    title = req.get('title')
    content = req.get('content')
    if not title or not content:
        return jsonify({'msg': 'Title and content required'}), 400

    data = load_data()
    post = {
        'id': data['next_post_id'],
        'title': title,
        'content': content,
        'author': current_user,
        'created_at': datetime.utcnow().isoformat(),
        'replies': []
    }
    data['posts'].append(post)
    data['next_post_id'] += 1
    save_data(data)
    return jsonify({'msg': 'Post created', 'post_id': post['id']}), 201

@app.route('/posts/<int:post_id>/replies', methods=['POST'])
@token_required
def add_reply(current_user, post_id):
    req = request.get_json()
    content = req.get('content')
    if not content:
        return jsonify({'msg': 'Reply content required'}), 400

    data = load_data()
    for post in data['posts']:
        if post['id'] == post_id:
            reply = {
                'id': len(post['replies']) + 1,
                'author': current_user,
                'content': content,
                'created_at': datetime.utcnow().isoformat()
            }
            post['replies'].append(reply)
            save_data(data)
            return jsonify({'msg': 'Reply added'}), 201
    return jsonify({'msg': 'Post not found'}), 404

@app.route('/posts', methods=['GET'])
def get_posts():
    data = load_data()
    posts = sorted(data['posts'], key=lambda p: p['created_at'], reverse=True)
    result = []
    for p in posts:
        result.append({
            'id': p['id'],
            'title': p['title'],
            'author': p['author'],
            'created_at': p['created_at'],
            'reply_count': len(p['replies'])
        })
    return jsonify(result), 200

@app.route('/posts/<int:post_id>', methods=['GET'])
def get_post(post_id):
    data = load_data()
    for post in data['posts']:
        if post['id'] == post_id:
            return jsonify(post), 200
    return jsonify({'msg': 'Post not found'}), 404

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)