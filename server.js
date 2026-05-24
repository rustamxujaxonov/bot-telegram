const express = require('express');
const mongoose = require('mongoose');
const cors = require('cors');
const helmet = require('helmet');
require('dotenv').config();
require('./bot.js');

const app = express();

app.use(helmet());
app.use(cors({ origin: process.env.FRONTEND_URL || '*' }));
app.use(express.json());

// Routes
app.use('/api/auth', require('./routes/auth'));
app.use('/api/stages', require('./routes/stages'));
app.use('/api/users', require('./routes/users'));
app.use('/api/leaderboard', require('./routes/leaderboard'));

mongoose.connect(process.env.MONGODB_URI)
  .then(() => console.log('MongoDB ulandi'))
  .catch(err => console.error('MongoDB xatolik:', err));

const PORT = process.env.PORT || 3001;
app.listen(PORT, () => console.log(`Server: http://localhost:${PORT}`));
