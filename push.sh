#!/bin/bash

# Ask for branch name
read -p "Branch name [master]: " branch

# Default to master
branch=${branch:-master}

# Handle branch
if git show-ref --verify --quiet "refs/heads/$branch"; then
    echo "Branch '$branch' already exists. Switching to it..."
    git checkout "$branch"
else
    echo "Creating branch '$branch'..."
    git checkout -b "$branch"
fi

# Ask for commit message
read -p "Commit message: " commit_message

# Make sure commit message isn't empty
if [ -z "$commit_message" ]; then
    echo "Commit message cannot be empty."
    exit 1
fi

# Add changes
echo "Adding changes..."
git add .

# Commit
echo "Committing..."
git commit -m "$commit_message"

# Push
echo "Pushing to origin/$branch..."
git push -u origin "$branch" --force

echo "Done! 🚀"