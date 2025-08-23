const express = require('express');
const router = express.Router();
const Mcq = require('../models/mcq');

router.post('/add', async (req, res) => {
  const { subject, chapter, qs, options, ans, explanation } = req.body;

  if (!subject || !chapter || !qs || !options || !ans) {
    return res.status(400).json({ error: 'সব তথ্য দিন' });
  }

  try {
    const mcq = new Mcq({ subject, chapter, qs, options, ans, explanation });
    await mcq.save();
    res.json({ message: 'MCQ যুক্ত হয়েছে' });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'সার্ভার সমস্যা' });
  }
});

router.get('/all', async (req, res) => {
  try {
    const data = await Mcq.find().sort({ _id: 1 });  // sr না থাকলে _id দিয়ে sort করো
    res.json(data);
  } catch (err) {
    res.status(500).json({ error: 'তথ্য আনতে সমস্যা হয়েছে' });
  }
});




// Route: /api/mcq/subjects
router.get('/subjects', async (req, res) => {
  try {
    const subjects = await Mcq.distinct('subject');
    res.json(subjects);
  } catch (err) {
    res.status(500).json({ error: 'সাবজেক্ট আনতে সমস্যা হয়েছে' });
  }
});



// Route: /api/mcq/chapters/:subject
router.get('/chapters/:subject', async (req, res) => {
  try {
    const chapters = await Mcq.find({ subject: req.params.subject }).distinct('chapter');
    res.json(chapters);
  } catch (err) {
    res.status(500).json({ error: 'অধ্যায় আনতে সমস্যা হয়েছে' });
  }
});


// Route: /api/mcq/by-chapter?subject=math&chapter=math-1
router.get('/by-chapter', async (req, res) => {
  const { subject, chapter } = req.query;
  try {
    const mcqs = await Mcq.find({ subject, chapter });
    res.json(mcqs);
  } catch (err) {
    res.status(500).json({ error: 'MCQ আনতে সমস্যা হয়েছে' });
  }
});
 // ===============



 // Route: /api/mcq/update/:id
router.put('/update/:id', async (req, res) => {
  const { subject, chapter, qs, options, ans, explanation } = req.body;
  if (!qs || !options || !ans) {
    return res.status(400).json({ error: 'সকল তথ্য দিন' });
  }

  try {
    const updated = await Mcq.findByIdAndUpdate(req.params.id, {
      subject, chapter, qs, options, ans, explanation
    }, { new: true });

    if (!updated) return res.status(404).json({ error: 'MCQ খুঁজে পাওয়া যায়নি' });

    res.json({ message: 'MCQ আপডেট হয়েছে', mcq: updated });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'আপডেট করতে সমস্যা হয়েছে' });
  }
});
// Route: /api/mcq/delete/:id
// Route: /api/mcq/delete/:id
router.delete('/delete/:id', async (req, res) => {
  try {
    const deleted = await Mcq.findByIdAndDelete(req.params.id);
    if (!deleted) {
      return res.status(404).json({ error: 'MCQ খুঁজে পাওয়া যায়নি' });
    }
    res.json({ message: 'MCQ মুছে ফেলা হয়েছে' });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'মুছতে সমস্যা হয়েছে' });
  }
});



// উদাহরণ Express API রুট
// ✅ সঠিকভাবে router ব্যবহার করা হচ্ছে
router.get('/questions', async (req, res) => {
  const { subject, chapter, limit } = req.query;
  const limitNum = parseInt(limit) || 20;

  try {
    const questions = await Mcq.find({ subject, chapter }).limit(limitNum);
    res.json(questions);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'প্রশ্ন আনতে সমস্যা হয়েছে' });
  }
});



// Route: /api/mcq/combined-test (multiple subjects)
// এই রুটটি "বিভিন্ন সাবজেক্ট" থেকে প্রশ্ন নিয়ে একটি কম্বাইন্ড টেস্ট তৈরি করবে
const MCQ = require('../models/mcq'); // MCQ মডেলটি এখানে ব্যবহার
router.post('/combined-test', async (req, res) => {
  const subjectConfig = req.body.subjectConfig;
  if (!subjectConfig || typeof subjectConfig !== 'object') {
    return res.status(400).json({ error: 'Invalid subjectConfig' });
  }

  try {
    let finalQuestions = [];

    for (const [subject, limit] of Object.entries(subjectConfig)) {
      const questions = await MCQ.aggregate([
        { $match: { subject } },
        { $sample: { size: limit } }
      ]);
      finalQuestions.push(...questions);
    }

    // Optional shuffle
    finalQuestions = finalQuestions.sort(() => 0.5 - Math.random());

    res.json(finalQuestions);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to load combined MCQ' });
  }
});





module.exports = router;
